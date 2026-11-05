from fnmusic.config import Config, load_env


def test_environment_file_is_data_not_shell(tmp_path, monkeypatch):
    config = tmp_path / ".env"
    config.write_text(
        "LX_PASSWORD='$(echo never-run)'\nPLAY_MODE=proxy\nPUBLIC_PLAYLISTS='[]'\n"
    )
    monkeypatch.delenv("LX_PASSWORD", raising=False)
    monkeypatch.delenv("PLAY_MODE", raising=False)
    monkeypatch.delenv("PUBLIC_PLAYLISTS", raising=False)
    load_env(config)
    c = Config.from_env()
    assert c.lx_password == "$(echo never-run)"
    assert c.play_mode == "proxy"
    monkeypatch.delenv("LX_PASSWORD")
    monkeypatch.delenv("PLAY_MODE")
    monkeypatch.delenv("PUBLIC_PLAYLISTS")
