"""Native FPK lifecycle. Only this module installs application-owned service units."""

import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import tarfile
import time

from fnmusic.control import atomic_text
from fnmusic.logs import event
from scripts.runtime import configure

UNITS = (
    "fnmusic-lx-proxy.service",
    "fnmusic-lx-manager.service",
    "fnmusic-lx-source.service",
)
MARKER = "# Managed by fnmusic-lx-first FPK"


class ServiceOperationError(RuntimeError):
    def __init__(self, action, units, code):
        self.action, self.units, self.code = action, units, code
        super().__init__("Application service operation failed")


def quote(value):
    return (
        '"'
        + str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
        + '"'
    )


def env_values(path):
    result = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            tokens = shlex.split(value)
            result[key] = tokens[0] if tokens else ""
    return result


def initialize(root, state):
    root, state = Path(root).resolve(), Path(state).resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    values = env_values(state / "worker.env")
    # Upgrade only the previous shipped default; retain custom source choices.
    if values.get("LX_PLATFORMS") == "kw,kg,tx,wy,mg":
        values["LX_PLATFORMS"] = "kg,tx,kw,wy,mg"
        if values.get("HOT_SOURCE") == "kw" and not values.get("HOT_BOARD"):
            values["HOT_SOURCE"] = "kg"
    if values.get("HOT_SOURCE") == "kg" and not values.get("HOT_BOARD"):
        values["HOT_SOURCE"] = "kg,tx"
    defaults = {
        "LX_PASSWORD": secrets.token_urlsafe(32),
        "LX_PLATFORMS": "kg,tx,kw,wy,mg",
        "DOWNLOAD_DIR": str(state / "unconfigured"),
        "PLAY_MODE": "direct",
        "PLAY_QUALITY": "320k",
        "DEFAULT_PLAYLIST_GUID": "",
        "SCAN_PATH": "",
        "SCAN_BODY": "{}",
        "PUBLIC_PLAYLISTS": "[]",
        "HOT_SOURCE": "kg,tx",
        "HOT_BOARD": "",
    }
    for key, value in defaults.items():
        values.setdefault(key, value)
    values.update(
        APP_ROOT=str(root),
        DATA_DIR=str(state),
        UPSTREAM_SOCKET="/var/run/trim_music.original.socket",
        LISTEN_SOCKET="/var/run/fnmusic-lx.socket",
        LX_URL="http://127.0.0.1:18775",
    )
    atomic_text(
        state / "worker.env",
        "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()),
    )
    if not (state / "settings.json").exists():
        atomic_text(
            state / "settings.json",
            json.dumps(
                {
                    "takeover": False,
                    "download_dir": "",
                    "play_mode": "direct",
                    "source_id": "",
                    "source_name": "未配置音源",
                    "source_version": "",
                },
                ensure_ascii=False,
            ),
        )
    lxstate = state / "lxserver"
    lxstate.mkdir(exist_ok=True, mode=0o700)
    if not (lxstate / "config.js").exists():
        configure(lxstate / "config.js", values["LX_PASSWORD"], 18775)


def render_units(root, state):
    root, state = Path(root).resolve(), Path(state).resolve()
    py = quote(root / "runtime/python/bin/python3")
    node = quote(root / "runtime/node/bin/node")
    env = state / "worker.env"
    code = root / "code"
    # WorkingDirectory is a literal path, not an ExecStart/Environment word.
    # systemd retains enclosing quotes here and rejects the path as non-absolute.
    working_directory = str(code).replace("%", "%%")
    shared = f"{MARKER}\n[Unit]\nAfter=network.target\n\n[Service]\nType=simple\nWorkingDirectory={working_directory}\nEnvironment={quote('PYTHONPATH=' + str(root / 'site') + ':' + str(code))}\nEnvironment={quote('PATH=' + str(root / 'runtime/node/bin') + ':' + str(root / 'runtime/python/bin') + ':/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')}\nUMask=0077\n"
    source = (
        shared
        + f"Environment={quote('DATA_PATH=' + str(state / 'lxserver'))}\nEnvironment={quote('CONFIG_PATH=' + str(state / 'lxserver/config.js'))}\nEnvironment=PORT=18775\nEnvironment=BIND_IP=127.0.0.1\nExecStart={node} {quote(root / 'lxserver/index.js')}\nRestart=on-failure\nRestartSec=5\n"
    )
    manager = (
        shared
        + f"ExecStart={py} -m fnmusic.manager --env {quote(env)} --socket {quote(root / 'fnmusic-lx-first.sock')}\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=20\nKillMode=control-group\n"
    )
    proxy = (
        shared
        + f"ExecStart={py} -m fnmusic --env {quote(env)}\nExecStartPost={py} -m scripts.wait_ready --socket /var/run/fnmusic-lx.socket\nExecStartPost={py} -m scripts.socket_control enable --state {quote(state / 'takeover.json')}\nExecStop={py} -m scripts.socket_control restore --state {quote(state / 'takeover.json')}\nExecStopPost={py} -m scripts.socket_control restore --state {quote(state / 'takeover.json')}\nRestart=no\nTimeoutStopSec=30\n"
    )
    return dict(
        zip(
            (
                "fnmusic-lx-source.service",
                "fnmusic-lx-manager.service",
                "fnmusic-lx-proxy.service",
            ),
            (source, manager, proxy),
        )
    )


def systemctl(action, *units, check=True):
    result = subprocess.run(
        ["systemctl", action, *units],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )
    if check and result.returncode:
        raise ServiceOperationError(action, units, result.returncode)
    return result.returncode == 0


def install(root, state):
    initialize(root, state)
    for name, text in render_units(root, state).items():
        dest = Path("/etc/systemd/system") / name
        if dest.exists() and MARKER not in dest.read_text():
            raise RuntimeError("Service name is owned by another installation")
        atomic_text(dest, text)
        dest.chmod(0o644)
    systemctl("daemon-reload")


def start(root, state):
    systemctl("start", "fnmusic-lx-source.service")
    systemctl("start", "fnmusic-lx-manager.service")
    preferences = json.loads((state / "settings.json").read_text())
    if preferences.get("takeover"):
        if not systemctl("start", "fnmusic-lx-proxy.service", check=False):
            preferences["takeover"] = False
            atomic_text(
                state / "settings.json", json.dumps(preferences, ensure_ascii=False)
            )


def stop(state=None):
    # Stop management first so an in-flight setting transaction cannot restart
    # the proxy after it has already been restored by this shutdown operation.
    failures = []
    for unit in (
        "fnmusic-lx-manager.service",
        "fnmusic-lx-proxy.service",
        "fnmusic-lx-source.service",
    ):
        try:
            stopped = systemctl("stop", unit, check=False)
        except (OSError, subprocess.SubprocessError):
            stopped = False
        if not stopped:
            try:
                # is-active is also false while a unit is deactivating. Queue a
                # stop (disables restart), then kill its entire cgroup regardless.
                queued = systemctl("stop", "--no-block", unit, check=False)
                killed = systemctl(
                    "kill", "--signal=SIGKILL", "--kill-whom=all", unit, check=False
                )
                if (
                    not queued
                    or not killed
                    or systemctl("is-active", unit, check=False)
                ):
                    failures.append(unit)
            except (OSError, subprocess.SubprocessError):
                failures.append(unit)
    if state is not None and (Path(state) / "takeover.json").exists():
        from scripts.socket_control import restore

        try:
            restore(
                Path("/var/run/trim_music.socket"),
                Path("/var/run/trim_music.original.socket"),
                Path("/var/run/fnmusic-lx.socket"),
                Path(state) / "takeover.json",
            )
        except Exception:
            failures.append("official socket restore")
    if failures:
        raise RuntimeError("Some application services could not be stopped safely")


def uninstall(state):
    stop(state)
    # App Center removes the application data directory. Keep a private recovery
    # archive on the storage volume; user music lives outside state and is untouched.
    parts = state.resolve().parts
    backup_root = (
        Path("/" + parts[1])
        if len(parts) > 1 and parts[1].startswith("vol")
        else state.parent
    )
    archive = (
        backup_root / f"fnmusic-lx-first-backup-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    )
    with archive.open("xb") as output:
        os.chmod(archive, 0o600)
        with tarfile.open(fileobj=output, mode="w:gz") as tar:
            tar.add(state, arcname="settings", recursive=True)
    for name in UNITS:
        path = Path("/etc/systemd/system") / name
        if path.exists() and MARKER in path.read_text():
            path.unlink()
    systemctl("daemon-reload")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("install", "start", "stop", "status", "upgrade-init", "uninstall"),
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--state", required=True)
    args = parser.parse_args()
    root, state = Path(args.root).resolve(), Path(args.state).resolve()
    try:
        if args.action != "status":
            event(state, "lifecycle", args.action + ".started")
        if args.action == "install":
            install(root, state)
        elif args.action == "start":
            start(root, state)
        elif args.action in ("stop", "upgrade-init"):
            stop(state)
        elif args.action == "uninstall":
            uninstall(state)
        elif args.action == "status":
            raise SystemExit(
                0
                if systemctl("is-active", "fnmusic-lx-manager.service", check=False)
                else 3
            )
        event(state, "lifecycle", args.action + ".completed")
    except Exception as exc:
        details = (
            {"action": exc.action, "units": ",".join(exc.units), "code": exc.code}
            if isinstance(exc, ServiceOperationError)
            else {}
        )
        event(
            state,
            "lifecycle",
            args.action + ".failed",
            level="ERROR",
            error=exc,
            **details,
        )
        reason = (
            f"systemctl {exc.action} {','.join(exc.units)} exited {exc.code}"
            if isinstance(exc, ServiceOperationError)
            else type(exc).__name__
        )
        parser.exit(
            1,
            f"fnMusic {args.action} failed: {reason}. Logs: {state / 'logs'}.\n",
        )


if __name__ == "__main__":
    main()
