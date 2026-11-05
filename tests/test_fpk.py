import json
from scripts.fpk_lifecycle import initialize, render_units
import scripts.fpk_lifecycle as lifecycle
import subprocess
import pytest


def test_fpk_initialization_keeps_takeover_disabled_and_no_source(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    state = tmp_path / "var"
    initialize(root, state)
    prefs = json.loads((state / "settings.json").read_text())
    assert prefs["takeover"] is False and prefs["download_dir"] == ""
    assert not (state / "private-source.js").exists()
    assert "LX_PASSWORD=" in (state / "worker.env").read_text()
    assert (state / "worker.env").stat().st_mode & 0o777 == 0o600
    before = (state / "worker.env").read_bytes()
    prefs["download_dir"] = "/vol1/music"
    (state / "settings.json").write_text(json.dumps(prefs))
    initialize(root, state)
    assert (state / "worker.env").read_bytes() == before
    assert (
        json.loads((state / "settings.json").read_text())["download_dir"]
        == "/vol1/music"
    )


def test_units_only_manager_and_source_start_by_default(tmp_path):
    units = render_units(tmp_path / "app with spaces", tmp_path / "var")
    assert set(units) == {
        "fnmusic-lx-source.service",
        "fnmusic-lx-manager.service",
        "fnmusic-lx-proxy.service",
    }
    manager = units["fnmusic-lx-manager.service"]
    assert "scripts.socket_control enable" not in manager
    assert "fnmusic.manager" in manager
    proxy = units["fnmusic-lx-proxy.service"]
    assert "scripts.socket_control enable" in proxy
    assert "scripts.socket_control restore" in proxy
    assert 'ExecStart="' in proxy


def test_manager_stop_timeout_does_not_skip_official_restore(monkeypatch):
    calls = []

    def control(action, *units, **kwargs):
        calls.append((action, units))
        if action == "stop" and units == ("fnmusic-lx-manager.service",):
            raise subprocess.TimeoutExpired("systemctl", 60)
        return False if action == "is-active" else True

    monkeypatch.setattr(lifecycle, "systemctl", control)
    lifecycle.stop()
    assert ("stop", ("fnmusic-lx-proxy.service",)) in calls
    assert ("stop", ("fnmusic-lx-source.service",)) in calls


def test_failed_force_stop_cannot_report_success(monkeypatch):
    calls = []

    def control(action, *units, **kwargs):
        calls.append((action, units))
        if units and units[-1] == "fnmusic-lx-manager.service":
            return False
        return True

    monkeypatch.setattr(lifecycle, "systemctl", control)
    with pytest.raises(RuntimeError):
        lifecycle.stop()
    assert ("stop", ("fnmusic-lx-proxy.service",)) in calls
    assert ("stop", ("fnmusic-lx-source.service",)) in calls
