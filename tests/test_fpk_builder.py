import json
from scripts.build_fpk import skeleton


def test_fpk_skeleton_and_no_private_inputs(tmp_path):
    stage = tmp_path / "stage"
    skeleton(stage, "arm")
    assert "platform = arm" in (stage / "manifest").read_text()
    assert (stage / "ICON.PNG").read_bytes().startswith(b"\x89PNG")
    entry = json.loads((stage / "app/ui/config").read_text())[".url"][
        "fnmusic-lx-first.main"
    ]
    assert entry["gatewaySocket"] == "fnmusic-lx-first.sock"
    assert (stage / "app/code/fnmusic/static/index.html").exists()
    for kind in ('hot', 'explore', 'archaeology', 'default'):
        assert (stage / f'app/code/fnmusic/static/playlist-{kind}-v1.png').is_file()
    assert not list(stage.rglob("*music-source-paid*"))
    assert not list(stage.rglob(".env"))
    assert (stage / "cmd/main").stat().st_mode & 0o111


def test_repack_skeleton_removes_previously_bundled_ffmpeg(tmp_path):
    stage = tmp_path / "stage"
    old = stage / "app/runtime/ffmpeg/bin"
    old.mkdir(parents=True)
    (old / "ffmpeg").write_text("old bundled executable")
    skeleton(stage, "x86")
    assert not (stage / "app/runtime/ffmpeg").exists()
    assert (stage / "app/code/fnmusic/ffmpeg-assets.json").is_file()


def test_desktop_entry_changes_cache_key_for_each_package_version(tmp_path):
    from scripts.build_fpk import VERSION

    skeleton(tmp_path / 'stage', 'x86')
    entry = json.loads((tmp_path / 'stage/app/ui/config').read_text())['.url']['fnmusic-lx-first.main']
    assert entry['url'] == '/app/fnmusic-lx-first/?v=' + VERSION
    assert entry['gatewayPrefix'] == '/app/fnmusic-lx-first'


def test_future_version_renders_manifest_and_desktop_without_rewriting_sources(tmp_path, monkeypatch):
    import scripts.build_fpk as builder

    monkeypatch.setattr(builder, 'VERSION', '9.8.7')
    for arch in ('arm', 'x86'):
        stage = tmp_path / arch
        skeleton(stage, arch)
        manifest = (stage / 'manifest').read_text()
        assert 'version = 9.8.7\n' in manifest
        assert '@VERSION@' not in manifest and '@ARCH@' not in manifest
        entry = json.loads((stage / 'app/ui/config').read_text())['.url']['fnmusic-lx-first.main']
        assert entry['url'].endswith('?v=9.8.7')
        assert (stage / 'app/code/fnmusic/static/index.html').read_bytes() == (
            builder.ROOT / 'fnmusic/static/index.html').read_bytes()
