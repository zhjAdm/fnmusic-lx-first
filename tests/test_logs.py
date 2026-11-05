import json

import pytest

from fnmusic import logs


def test_event_log_is_private_bounded_and_never_records_exception_messages(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(logs, "MAX_BYTES", 800)
    for n in range(30):
        logs.event(
            tmp_path,
            "manager",
            "source.failed",
            level="ERROR",
            error=ValueError("paid-token-secret https://secret.test/a?key=secret"),
            status=422,
        )
    folder = tmp_path / "logs"
    assert (folder / "events.jsonl").stat().st_mode & 0o777 == 0o600
    assert folder.stat().st_mode & 0o777 == 0o700
    files = list(folder.glob("events.jsonl*"))
    assert len(files) <= 4
    assert all(p.stat().st_size < 1100 for p in files)
    text = "".join(p.read_text() for p in files)
    assert "paid-token-secret" not in text and "secret.test" not in text
    entry = json.loads((folder / "events.jsonl").read_text().splitlines()[-1])
    assert entry["error"]["type"] == "ValueError"


def test_log_tail_filters_limits_and_rejects_unknown_source(tmp_path):
    logs.event(tmp_path, "manager", "settings.saved")
    logs.event(tmp_path, "proxy", "job.failed", level="ERROR")
    result = logs.read_events(tmp_path, level="ERROR", limit=1)
    assert len(result) == 1 and result[0]["event"] == "job.failed"
    assert logs.read_events(tmp_path, level="INFO")[0]["event"] == "settings.saved"
    with pytest.raises(ValueError):
        logs.validate_query("../../worker.env", "ALL", 100)
    with pytest.raises(ValueError):
        logs.validate_query("events", "ALL", 100000)


def test_diagnostics_redacts_credentials_urls_and_control_sequences():
    value = 'Authorization: Bearer secret\nCookie: session=abc; csrf=xyz\nLX_PASSWORD=hello\n{"token":"xyz","password":"pqr"}\nurl=https://host/a?signature=signed\n\x1b[31mFailed to start\x1b[0m'
    result = logs.redact(value, secrets=("unlabelled-secret",))
    for secret in (
        "Bearer secret",
        "session=abc",
        "hello",
        "xyz",
        "pqr",
        "https://host",
        "signed",
        "\x1b",
    ):
        assert secret not in result
    assert "Failed to start" in result


@pytest.mark.asyncio
async def test_service_diagnostics_only_reads_fixed_units_and_pid1(monkeypatch):
    import asyncio

    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return (
                b'{"_PID":"1","MESSAGE":"WorkingDirectory= path is not absolute","PRIORITY":"3","UNIT":"fnmusic-lx-manager.service"}\n',
                b"",
            )

    async def spawn(*args, **kwargs):
        calls.append(args)
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = await logs.read_services(level="ERROR", limit=50)
    assert "_PID=1" in calls[0]
    assert "fnmusic-lx-source.service" in calls[0]
    assert len(result["entries"]) == 1
    assert "not absolute" in result["entries"][0]["message"]


@pytest.mark.asyncio
async def test_bootstrap_export_remains_valid_with_embedded_json_and_known_secret(
    tmp_path,
):
    folder = tmp_path / "logs"
    folder.mkdir()
    (folder / "bootstrap.log").write_text(
        'Error {"token":"private", "url":"https://host/a?key=x"} known-secret\n'
    )
    result = await logs.collect(tmp_path, source="bootstrap", secrets=("known-secret",))
    text = json.dumps(result, ensure_ascii=False)
    assert (
        "private" not in text
        and "known-secret" not in text
        and "https://host" not in text
    )
    assert "Error" in logs.format_text(result)


@pytest.mark.asyncio
async def test_service_export_rejects_non_pid1_records_even_if_journal_returns_them(
    monkeypatch,
):
    import asyncio

    class Process:
        returncode = 0

        async def communicate(self):
            return (
                b'{"_PID":"999","MESSAGE":"source-private-secret","PRIORITY":"3"}\n{ "_PID":"1", "MESSAGE":"Failed to start service", "PRIORITY":"3"}\n',
                b"",
            )

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = await logs.read_services()
    assert len(result["entries"]) == 1
    assert "source-private-secret" not in json.dumps(result)


def test_logging_failure_cannot_break_business_operation(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(logs.os, "open", denied)
    logs.event(tmp_path, "manager", "settings.saved")
