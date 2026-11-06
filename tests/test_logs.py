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


def test_timeout_diagnostic_contains_upstream_and_safe_route_not_query_or_secrets(
    tmp_path,
):
    import httpx

    request = httpx.Request(
        "GET",
        "http://official/music/api/v1/search/track?q=private-keyword&token=secret",
        extensions={"fnmusic_upstream": "official"},
    )
    error = httpx.ReadTimeout("https://secret.test/?key=never-log", request=request)
    logs.event(tmp_path, "proxy", "upstream.failed", level="ERROR", error=error)
    row = logs.read_events(tmp_path)[0]["error"]
    assert row["upstream"] == "official"
    assert row["method"] == "GET"
    assert row["route"] == "/music/api/v1/search/track"
    text = json.dumps(row)
    assert "secret" not in text and "private-keyword" not in text and "?q=" not in text


def test_remote_media_timeout_never_records_url_path():
    import httpx

    request = httpx.Request(
        "GET",
        "https://host/private-signature/song.mp3?key=secret",
        extensions={"fnmusic_upstream": "media"},
    )
    row = logs.error_summary(httpx.ReadTimeout("failure", request=request))
    assert row["upstream"] == "media"
    assert "private-signature" not in json.dumps(row)
    assert row["route"] == "[remote-media]"


@pytest.mark.asyncio
async def test_activity_filter_only_returns_search_and_play_events(tmp_path):
    logs.event(tmp_path, "history", "search.completed", keyword="青花瓷", total=5)
    logs.event(tmp_path, "manager", "settings.save")
    logs.event(tmp_path, "history", "play.redirect", title="青花瓷")
    result = await logs.collect(tmp_path, source="history")
    assert len(result["entries"]) == 2
    assert result["entries"][0]["event"] == "play.redirect"
    assert result["entries"][1]["keyword"] == "青花瓷"
    assert all(r["component"] == "history" for r in result["entries"])


@pytest.mark.asyncio
async def test_log_display_returns_latest_matches_first_across_rotated_files(tmp_path):
    folder = tmp_path / 'logs'
    folder.mkdir()
    def row(name, level='INFO'):
        return json.dumps({'time': '2000-01-01T00:00:00+00:00', 'component': 'manager', 'event': name, 'level': level}) + '\n'
    (folder / 'events.jsonl.1').write_text(row('oldest') + row('older'))
    (folder / 'events.jsonl').write_text(row('newer') + row('warning', 'WARNING') + row('newest'))
    result = await logs.collect(tmp_path, level='INFO', limit=3)
    assert [r['event'] for r in result['entries']] == ['newest', 'newer', 'older']
    assert (folder / 'events.jsonl.1').read_text().startswith(row('oldest'))
    text = logs.format_text(result)
    assert text.index('newest') < text.index('newer') < text.index('older')


@pytest.mark.asyncio
async def test_service_log_display_is_newest_first(tmp_path, monkeypatch):
    class Process:
        returncode = 0
        async def communicate(self):
            return (b'\n'.join(json.dumps({'_PID': '1', 'MESSAGE': message, '__REALTIME_TIMESTAMP': timestamp}).encode()
                               for message, timestamp in [('older', 1000000), ('newer', 2000000)]), b'')
    async def spawn(*args, **kwargs):
        return Process()
    monkeypatch.setattr(logs.asyncio, 'create_subprocess_exec', spawn)
    result = await logs.collect(tmp_path, source='services')
    assert [r['message'] for r in result['entries']] == ['newer', 'older']


@pytest.mark.asyncio
async def test_bootstrap_display_reverses_failures_without_reversing_traceback_lines(tmp_path):
    folder = tmp_path / 'logs'
    folder.mkdir()
    (folder / 'bootstrap.log').write_text(
        '2026-11-05T01:00:00Z startup failure\nold traceback\nold error\n'
        '2026-11-06T01:00:00Z startup failure\nnew traceback\nnew error\n'
    )
    result = await logs.collect(tmp_path, source='bootstrap')
    assert len(result['entries']) == 2
    newest, older = result['entries']
    assert newest['message'] == '2026-11-06T01:00:00Z startup failure\nnew traceback\nnew error'
    assert 'old error' in older['message']
