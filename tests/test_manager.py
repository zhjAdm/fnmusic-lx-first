import json
from pathlib import Path
from fastapi.testclient import TestClient
from fnmusic.manager import create_manager
from fnmusic.config import Config

ADMIN = {"X-Trim-Isadmin": "true", "X-Fnmusic-Request": "1"}
SCRIPT = "/*! @name My source\n * @version 1.0 */\nconst lx=globalThis.lx;"


class Control:
    active = False
    starts = 0

    async def running(self):
        return self.active

    async def enable(self):
        self.active = True
        self.starts += 1

    async def disable(self):
        self.active = False


class LX:
    async def import_script(self, path):
        return "test-source"

    async def call(self, *args, **kwargs):
        return {"success": True}

    async def qualities(self, source):
        return ["320k"]


def manager(tmp_path):
    config = Config(
        data_dir=tmp_path / "state", download_dir=tmp_path / "not-configured"
    )
    env = tmp_path / "worker.env"
    env.write_text("LX_PASSWORD='fixture-private'\n")
    control = Control()
    app = create_manager(
        config, env, control, LX(), validate_dir=lambda p: Path(p).resolve()
    )
    return app, control


def test_manager_denies_non_admin_and_mutation_without_csrf_header(tmp_path):
    app, _ = manager(tmp_path)
    with TestClient(app) as c:
        assert c.get("/api/state").status_code == 403
        assert (
            c.post(
                "/api/takeover",
                json={"enabled": False},
                headers={"X-Trim-Isadmin": "true"},
            ).status_code
            == 403
        )


def test_initial_state_does_not_take_over_and_requires_configuration(tmp_path):
    app, control = manager(tmp_path)
    with TestClient(app) as c:
        state = c.get("/api/state", headers=ADMIN).json()
        assert state["takeover"] is False and not state["configured"]
        assert control.starts == 0
        assert (
            c.post("/api/takeover", json={"enabled": True}, headers=ADMIN).status_code
            == 409
        )


def test_ui_configuration_and_takeover_toggle(tmp_path):
    app, control = manager(tmp_path)
    with TestClient(app) as c:
        assert (
            c.post(
                "/api/source",
                json={"name": "private.js", "content": SCRIPT},
                headers=ADMIN,
            ).status_code
            == 200
        )
        assert (
            c.post(
                "/api/settings",
                json={"download_dir": str(tmp_path / "music"), "play_mode": "proxy"},
                headers=ADMIN,
            ).status_code
            == 200
        )
        assert (
            c.post("/api/takeover", json={"enabled": True}, headers=ADMIN).status_code
            == 200
        )
        assert control.active
        state = c.get("/app/fnmusic-lx-first/api/state", headers=ADMIN).json()
        assert state["takeover"] and state["configured"]
        assert "content" not in json.dumps(state)
        assert (
            c.post("/api/takeover", json={"enabled": False}, headers=ADMIN).status_code
            == 200
        )
        assert not control.active
        assert (tmp_path / "state" / "private-source.js").read_text() == SCRIPT
        assert "DOWNLOAD_DIR" in (tmp_path / "worker.env").read_text()
        assert c.get("/", headers=ADMIN).status_code == 200


def test_rejects_ceru_and_strict_boolean(tmp_path):
    app, _ = manager(tmp_path)
    with TestClient(app) as c:
        assert (
            c.post(
                "/api/source",
                json={"name": "source.js", "content": "exports.manifest={}"},
                headers=ADMIN,
            ).status_code
            == 422
        )
        assert (
            c.post(
                "/api/takeover", json={"enabled": "false"}, headers=ADMIN
            ).status_code
            == 422
        )


def test_download_directory_cannot_be_application_data(tmp_path):
    app, _ = manager(tmp_path)
    with TestClient(app) as c:
        response = c.post(
            "/api/settings",
            json={
                "download_dir": str(tmp_path / "state" / "music"),
                "play_mode": "direct",
            },
            headers=ADMIN,
        )
        assert response.status_code == 422


def test_optional_component_download_requires_admin_and_is_not_automatic(
    tmp_path, monkeypatch
):
    app, _ = manager(tmp_path)
    from fnmusic.tools import MediaTools

    calls = []

    async def install(self):
        calls.append(True)

    monkeypatch.setattr(MediaTools, "install", install)
    with TestClient(app) as c:
        assert c.post("/api/tools/download", json={}).status_code == 403
        state = c.get("/api/state", headers=ADMIN).json()
        assert "audio_tools" in state
        assert calls == []
        assert c.post("/api/tools/download", json={}, headers=ADMIN).status_code == 200
    assert calls == [True]


def test_logs_admin_read_filter_export_and_invalid_sources(tmp_path):
    app, _ = manager(tmp_path)
    from fnmusic.logs import event

    event(tmp_path / "state", "manager", "settings.saved")
    event(
        tmp_path / "state",
        "jobs",
        "job.failed",
        level="ERROR",
        error=ValueError("private URL"),
    )
    with TestClient(app) as c:
        assert c.get("/api/logs").status_code == 403
        assert c.get("/api/logs/download").status_code == 403
        result = c.get("/api/logs?source=events&level=ERROR&limit=10", headers=ADMIN)
        assert result.status_code == 200
        assert len(result.json()["entries"]) == 1
        exported = c.get(
            "/app/fnmusic-lx-first/api/logs/download?source=events&level=ERROR",
            headers=ADMIN,
        )
        assert (
            exported.status_code == 200
            and "attachment" in exported.headers["content-disposition"]
        )
        assert "job.failed" in exported.text and "private URL" not in exported.text
        assert (
            c.get("/api/logs?source=../../worker.env", headers=ADMIN).status_code == 422
        )
        assert c.get("/api/logs?limit=100000", headers=ADMIN).status_code == 422
        assert exported.headers["cache-control"] == "no-store"
