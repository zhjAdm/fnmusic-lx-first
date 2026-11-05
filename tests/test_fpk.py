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


def test_working_directory_is_a_literal_absolute_path_not_a_quoted_word(tmp_path):
    # systemd's WorkingDirectory parser does not unquote shell-style words.
    # Quoted paths are rejected as non-absolute even when their contents start /.
    root = tmp_path / "app with spaces"
    for text in render_units(root, tmp_path / "var").values():
        line = next(
            line for line in text.splitlines() if line.startswith("WorkingDirectory=")
        )
        value = line.partition("=")[2]
        assert value == str(root.resolve() / "code")
        assert value.startswith("/")


def test_bootstrap_keeps_early_python_failure_and_exposes_appcenter_error(tmp_path):
    import os

    root = tmp_path / "app"
    binary = root / "runtime/python/bin/python3"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/bin/bash\nprintf '%*s' 1200000 '' >&2\necho \"Python dependency missing\" >&2\nexit 23\n"
    )
    binary.chmod(0o755)
    state = tmp_path / "var"
    tip = tmp_path / "tip.log"
    result = subprocess.run(
        ["bash", "packaging/fpk/cmd/main", "start"],
        env=dict(
            os.environ,
            TRIM_APPDEST=str(root),
            TRIM_PKGVAR=str(state),
            TRIM_TEMP_LOGFILE=str(tip),
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 23
    log = state / "logs/bootstrap.log"
    assert log.exists() and "Python dependency missing" in log.read_text()
    assert log.stat().st_size < 1024 * 1024
    assert log.stat().st_mode & 0o777 == 0o600
    assert "Python dependency missing" in tip.read_text()


def test_logging_storage_failure_does_not_prevent_lifecycle_stop(tmp_path):
    import os
    import shlex

    root = tmp_path / "app"
    binary = root / "runtime/python/bin/python3"
    binary.parent.mkdir(parents=True)
    marker = tmp_path / "stopped"
    binary.write_text("#!/bin/bash\ntouch " + shlex.quote(str(marker)) + "\nexit 0\n")
    binary.chmod(0o755)
    state = tmp_path / "var"
    state.mkdir()
    (state / "logs").write_text("blocks log directory creation")
    result = subprocess.run(
        ["bash", "packaging/fpk/cmd/main", "stop"],
        env=dict(os.environ, TRIM_APPDEST=str(root), TRIM_PKGVAR=str(state)),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and marker.exists()
