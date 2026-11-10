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


def test_discovery_settings_and_radar_are_admin_only_and_validate_input(tmp_path):
    app, _ = manager(tmp_path)
    with TestClient(app) as client:
        assert client.get('/api/discovery').status_code == 403
        assert client.get('/api/library/versions').status_code == 403
        assert client.get('/api/discovery', headers=ADMIN).json()['exploration_mode'] == 'balanced'
        assert client.post('/api/discovery', json={'exploration_mode': 'bold'},
                           headers={'X-Trim-Isadmin': 'true'}).status_code == 403
        saved = client.post('/api/discovery', json={'exploration_mode': 'bold'}, headers=ADMIN)
        assert saved.status_code == 200 and saved.json()['exploration_mode'] == 'bold'
        assert client.get('/api/discovery', headers=ADMIN).json() == saved.json()
        assert client.post('/api/discovery', json={'exploration_mode': 'bad'}, headers=ADMIN).status_code == 422
        assert client.post('/api/discovery', json={'exploration_mode': []}, headers=ADMIN).status_code == 422
        result = client.get('/api/library/versions', headers=ADMIN)
        assert result.status_code == 200 and result.json()['items'] == []
        assert client.get('/api/library/versions?page=0', headers=ADMIN).status_code == 422


def test_manual_recommendation_refresh_is_admin_only_and_metadata_only(tmp_path):
    from test_core import RAW

    class Hot:
        calls = 0
        broken = False

        async def hot(self, *args):
            self.calls += 1
            if self.broken:
                raise ValueError("unavailable")
            return [dict(RAW, songmid=str(self.calls))]

    app, _ = manager(tmp_path)
    lx = Hot()
    with TestClient(app) as client:
        path = '/app/fnmusic-lx-first/api/recommendations/refresh'
        assert client.post(path, json={}).status_code == 403
        assert client.post(path, json={}, headers={'X-Trim-Isadmin': 'true'}).status_code == 403
        assert client.get('/api/recommendations').status_code == 403
        app.state.recommendations.lx = lx
        initial = client.get('/api/recommendations', headers=ADMIN).json()
        assert initial['total'] == 0 and initial['updated_at'] == 0
        for count in (1, 2):
            result = client.post(path, json={}, headers=ADMIN)
            assert result.status_code == 200
            assert result.json()['total'] == 1 and result.json()['updated_at'] > 0
            assert lx.calls == count * 2
        saved = client.get('/api/recommendations', headers=ADMIN).json()
        lx.broken = True
        assert client.post(path, json={}, headers=ADMIN).status_code == 422
        assert client.get('/api/recommendations', headers=ADMIN).json() == saved
        store = app.state.recommendations.store
        assert store.db.execute('SELECT count(*) FROM job').fetchone()[0] == 0
        assert store.db.execute('SELECT count(*) FROM file').fetchone()[0] == 0


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


def test_library_scan_requires_admin_but_no_official_music_session(tmp_path):
    import time
    from library_fixtures import song

    app, _ = manager(tmp_path)
    music = tmp_path / "music"
    song(music)
    app.state.library.validator = lambda roots: roots
    with TestClient(app) as c:
        assert c.get("/api/library").status_code == 403
        assert (
            c.post(
                "/api/library",
                json={"directories": [str(music)], "auto_scan": True},
                headers=ADMIN,
            ).status_code
            == 200
        )
        assert c.post("/api/library/scan", json={}, headers=ADMIN).status_code == 200
        for _ in range(100):
            state = c.get("/api/library", headers=ADMIN).json()
            if state["scan"]["state"] == "completed":
                break
            time.sleep(0.01)
        assert state["scan"]["state"] == "completed"
        assert state["scan"]["indexed"] == 1
        assert c.post("/api/library/cancel", json={}, headers=ADMIN).status_code == 200


def test_library_song_listing_is_admin_only_and_validates_query(tmp_path):
    app, _ = manager(tmp_path)
    with TestClient(app) as c:
        assert c.get('/api/library/songs').status_code == 403
        result = c.get('/api/library/songs', headers=ADMIN)
        assert result.status_code == 200 and result.json()['total'] == 0
        assert result.headers['cache-control'] == 'no-store'
        assert c.get('/api/library/songs?page=0', headers=ADMIN).status_code == 422
        assert c.get('/api/library/songs?size=1000', headers=ADMIN).status_code == 422


def test_update_api_requires_admin_and_csrf_and_never_installs(tmp_path):
    app, _ = manager(tmp_path)
    with TestClient(app) as c:
        assert c.get('/api/update').status_code == 403
        state = c.get('/api/update', headers=ADMIN)
        assert state.status_code == 200 and state.headers['cache-control'] == 'no-store'
        assert state.json()['download']['state'] == 'idle'
        assert c.post('/api/update/check', json={}).status_code == 403
        assert c.post('/api/update/download', json={}, headers={'X-Trim-Isadmin': 'true'}).status_code == 403
        assert c.post('/api/update/download', json={}, headers=ADMIN).status_code == 422
        assert c.get('/api/update/file', headers=ADMIN).status_code == 422
        assert c.get('/api/update/file').status_code == 403


def test_upgrade_api_requires_admin_origin_and_explicit_confirmation(tmp_path):
    app, _ = manager(tmp_path)
    calls = []
    async def download(*, install=False):
        calls.append(install)
        return {'upgrade': {'state': 'downloading'}}
    app.state.updater.start_download = download
    with TestClient(app) as client:
        path = '/app/fnmusic-lx-first/api/update/install'
        assert client.post(path, json={'confirm': True}).status_code == 403
        assert client.post(path, json={'confirm': True}, headers={'X-Trim-Isadmin': 'true'}).status_code == 403
        assert client.post(path, json={}, headers=ADMIN).status_code == 422
        assert client.post(path, json={'confirm': 'true'}, headers=ADMIN).status_code == 422
        assert not calls
        assert client.post(path, json={'confirm': True}, headers=ADMIN).status_code == 200
        assert calls == [True]


def test_upgrade_blocks_configuration_mutations_but_keeps_status_readable(tmp_path):
    app, control = manager(tmp_path)
    app.state.updater.installer.state = lambda: {'supported': True, 'state': 'installing'}
    with TestClient(app) as client:
        response = client.post('/api/takeover', json={'enabled': False}, headers=ADMIN)
        assert response.status_code == 409
        assert '升级' in response.json()['error']
        assert client.get('/api/update', headers=ADMIN).status_code == 200
        assert control.starts == 0


def test_update_directory_api_saves_and_is_admin_only(tmp_path):
    app, control = manager(tmp_path)
    folder = tmp_path / 'packages'
    folder.mkdir()
    with TestClient(app) as client:
        url = '/app/fnmusic-lx-first/api/update/settings'
        assert client.post(url, json={'download_dir': str(folder)}).status_code == 403
        assert client.post(url, json={'download_dir': str(folder)}, headers={'X-Trim-Isadmin': 'true'}).status_code == 403
        saved = client.post(url, json={'download_dir': str(folder)}, headers=ADMIN)
        assert saved.status_code == 200
        assert saved.json()['download_dir'] == str(folder)
        assert client.get('/api/update', headers=ADMIN).json()['effective_dir'] == str(folder)
        assert client.post(url, json={}, headers=ADMIN).status_code == 422
        assert control.starts == 0


def test_verified_update_api_streams_checked_file_and_rejects_modified_package(tmp_path):
    import hashlib

    app, _ = manager(tmp_path)
    updater = app.state.updater
    updater.path.parent.mkdir(parents=True)
    package = b'checked-package'
    updater.path.write_bytes(package)
    updater.download = {'state': 'ready', 'filename': 'fixture.fpk', 'sha256': hashlib.sha256(package).hexdigest()}
    with TestClient(app) as client:
        response = client.get('/api/update/file', headers=ADMIN)
        assert response.status_code == 200 and response.content == package
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['content-disposition'] == 'attachment; filename="fixture.fpk"'
        updater.path.write_bytes(b'modified-package')
        assert client.get('/api/update/file', headers=ADMIN).status_code == 422


def test_manager_entry_and_assets_are_not_cached_and_state_reports_running_version(tmp_path):
    from fnmusic import __version__

    app, _ = manager(tmp_path)
    with TestClient(app) as client:
        for path in ('/', '/admin.js', '/admin.css', '/app/fnmusic-lx-first/'):
            response = client.get(path, headers=ADMIN)
            assert response.status_code == 200
            assert response.headers.get('cache-control') == 'no-store'
        response = client.get('/api/state', headers=ADMIN)
        assert response.headers.get('cache-control') == 'no-store'
        assert response.json()['app_version'] == __version__
