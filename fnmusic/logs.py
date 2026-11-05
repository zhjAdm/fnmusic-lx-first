"""Bounded, administrator-only diagnostics without request or script contents."""

import asyncio
from collections import deque
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import traceback

MAX_BYTES = 1024 * 1024
BACKUPS = 3
LEVELS = {"ALL", "INFO", "WARNING", "ERROR"}
UNITS = (
    "fnmusic-lx-source.service",
    "fnmusic-lx-manager.service",
    "fnmusic-lx-proxy.service",
)


def redact(text, secrets=()):
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[已隐藏]")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r'https?://[^\s<>"\']+', "[链接已隐藏]", text)
    text = re.sub(
        r"(?im)^(\s*(?:authorization|cookie|set-cookie|x-frontend-auth|x-access-code)\s*[:=]).*$",
        r"\1 [已隐藏]",
        text,
    )
    key = r"(?:password|passwd|pwd|token|access_token|refresh_token|lx_password|secret|api[_-]?key|密钥)"
    text = re.sub(
        r'(?i)(["\']?'
        + key
        + r'["\']?\s*[:=]\s*)(?:"[^"\n]*"|\'[^\'\n]*\'|[^\s,;}\]]+)',
        r'\1"[已隐藏]"',
        text,
    )
    return "".join(c for c in text if c in "\n\t" or ord(c) >= 32)[:16000]


def error_summary(error):
    result = {"type": type(error).__name__}
    # Exception messages may contain source URLs, signed query strings or script
    # output. Keep machine-readable causes and code locations, never str(error).
    if getattr(error, "errno", None) is not None:
        result["errno"] = error.errno
    response = getattr(error, "response", None)
    if response is not None:
        result["http_status"] = response.status_code
    if isinstance(error, ImportError) and error.name:
        result["module"] = str(error.name)[:120]
    result["frames"] = [
        {"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
        for f in traceback.extract_tb(error.__traceback__)[-6:]
    ]
    return result


def event(data_dir, component, name, *, level="INFO", error=None, **fields):
    """Best-effort append: a full disk must never break playback or shutdown."""
    try:
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", component + "." + name):
            return
        entry = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": level if level in LEVELS - {"ALL"} else "INFO",
            "component": component,
            "event": name,
        }
        allowed = {
            "operation",
            "status",
            "action",
            "units",
            "code",
            "attempts",
            "task",
            "quality",
            "state",
        }
        for key, value in fields.items():
            if key in allowed:
                entry[key] = (
                    value if type(value) in (int, bool) else redact(str(value))[:200]
                )
        if error is not None:
            entry["error"] = error_summary(error)
        encoded = (json.dumps(entry, ensure_ascii=False) + "\n").encode()
        folder = Path(data_dir) / "logs"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
        fd = os.open(
            folder / "write.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = folder / "events.jsonl"
            if path.exists() and path.stat().st_size + len(encoded) > MAX_BYTES:
                for n in range(BACKUPS, 0, -1):
                    source = folder / (
                        "events.jsonl" if n == 1 else f"events.jsonl.{n - 1}"
                    )
                    if source.exists():
                        source.replace(folder / f"events.jsonl.{n}")
            output = os.open(
                path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(output, "ab") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded)
    except (OSError, ValueError):
        pass


def validate_query(source, level, limit):
    if source not in ("events", "services", "bootstrap") or level not in LEVELS:
        raise ValueError("日志来源或级别无效")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("日志条数应为 1–500")


def tail_bytes(path, maximum=MAX_BYTES):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return ""
    with os.fdopen(fd, "rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - maximum))
        data = stream.read(maximum)
        if size > maximum:
            data = data.partition(b"\n")[2]
    return data.decode("utf-8", errors="replace")


def read_events(data_dir, level="ALL", limit=200):
    validate_query("events", level, limit)
    folder = Path(data_dir) / "logs"
    result = deque(maxlen=limit)
    for n in range(BACKUPS, -1, -1):
        path = folder / ("events.jsonl" if n == 0 else f"events.jsonl.{n}")
        try:
            text = tail_bytes(path)
        except OSError:
            continue
        for line in text.splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict) and (
                    level == "ALL" or row.get("level") == level
                ):
                    result.append(row)
            except ValueError:
                continue
    return list(result)


async def read_services(level="ALL", limit=200):
    validate_query("services", level, limit)
    process = None
    try:
        args = [
            "journalctl",
            "--no-pager",
            "--quiet",
            "--output=json",
            f"--lines={limit}",
        ]
        for unit in UNITS:
            args += ["--unit", unit]
        # Only PID1 lifecycle messages: no raw paid-source console output.
        process = await asyncio.create_subprocess_exec(
            *args,
            "_PID=1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await asyncio.wait_for(process.communicate(), 8)
        if process.returncode:
            return {
                "entries": [],
                "note": "系统服务日志暂不可用，请查看扩展日志或启动日志。",
            }
        rows = []
        for line in output[: 4 * 1024 * 1024].splitlines():
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or str(row.get("_PID")) != "1":
                    continue
                priority = int(row.get("PRIORITY", 6))
                severity = (
                    "ERROR" if priority <= 3 else "WARNING" if priority <= 4 else "INFO"
                )
                if level != "ALL" and level != severity:
                    continue
                rows.append(
                    {
                        "time": datetime.fromtimestamp(
                            int(row.get("__REALTIME_TIMESTAMP", 0)) / 1000000,
                            timezone.utc,
                        ).isoformat(timespec="seconds"),
                        "level": severity,
                        "component": "systemd",
                        "event": row.get("UNIT", "service"),
                        "message": redact(row.get("MESSAGE", "")),
                    }
                )
            except (ValueError, TypeError, OverflowError):
                continue
        return {
            "entries": rows[-limit:],
            "note": "" if rows else "暂无匹配的系统服务日志。",
        }
    except (OSError, TimeoutError):
        return {
            "entries": [],
            "note": "系统服务日志暂不可用，请查看扩展日志或启动日志。",
        }
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()


async def collect(data_dir, source="events", level="ALL", limit=200, secrets=()):
    validate_query(source, level, limit)
    if source == "services":
        result = await read_services(level, limit)
    elif source == "bootstrap":
        try:
            text = tail_bytes(Path(data_dir) / "logs/bootstrap.log", 256 * 1024)
        except OSError:
            text = ""
        rows = [
            {"level": "ERROR", "component": "bootstrap", "message": line}
            for line in text.splitlines()[-limit:]
        ]
        result = {
            "entries": rows if level in ("ALL", "ERROR") else [],
            "note": "" if text else "暂无启动失败日志。",
        }
    else:
        result = {"entries": read_events(data_dir, level, limit), "note": ""}

    # Apply known-credential removal to every field, including system diagnostics.
    def clean(value):
        if isinstance(value, str):
            return redact(value, secrets=secrets)
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        return value

    return clean(result)


def format_text(result):
    lines = []
    for row in result["entries"]:
        prefix = f"{row.get('time', '')} [{row.get('level', 'INFO')}] {row.get('component', '')} {row.get('event', '')}"
        detail = row.get("message") or json.dumps(
            {
                k: v
                for k, v in row.items()
                if k not in ("time", "level", "component", "event")
            },
            ensure_ascii=False,
        )
        lines.append(prefix + " " + detail)
    return (
        "\n".join(lines) + ("\n" + result["note"] if result.get("note") else "") + "\n"
    )
