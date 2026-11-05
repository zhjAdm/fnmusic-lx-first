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
    assert not list(stage.rglob("*music-source-paid*"))
    assert not list(stage.rglob(".env"))
    assert (stage / "cmd/main").stat().st_mode & 0o111
