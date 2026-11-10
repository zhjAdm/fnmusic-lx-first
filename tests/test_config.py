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


def test_default_platform_priority_is_kg_then_tx(monkeypatch):
    monkeypatch.delenv('LX_PLATFORMS', raising=False)
    monkeypatch.delenv('HOT_SOURCE', raising=False)
    monkeypatch.setenv('LX_PASSWORD', 'fixture')
    assert Config().platforms == ['kg', 'tx', 'kw', 'wy', 'mg']
    assert Config.from_env().platforms == ['kg', 'tx', 'kw', 'wy', 'mg']
    assert Config.from_env().hot_source == 'kg,tx'
