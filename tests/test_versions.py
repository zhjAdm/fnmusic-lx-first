import pytest

from fnmusic.config import Config
from fnmusic.library import Library
from library_fixtures import song


@pytest.mark.asyncio
async def test_version_radar_groups_only_recognized_suffixes_in_current_roots(tmp_path):
    from fnmusic.versions import radar

    config = Config(data_dir=tmp_path / "state")
    library = Library(config, validator=lambda roots: roots)
    root = tmp_path / "music"
    for i, (title, artist) in enumerate([
        ("Hello", "A"), ("Hello (Live)", "A"), ("Hello【伴奏】", "A"),
        ("Hello (Live)", "B"), ("Hello (Goodbye)", "A"),
        ("Other", "A"), ("Other - 2011 Remastered", "A"),
        ("Unknown", ""), ("Unknown (Live)", ""),
    ]):
        song(root, f"{i}.wav", title=title, artist=artist)
    library.save([str(root)], False)
    await library.scan()
    result = radar(library)
    assert result["total"] == 2
    hello = next(g for g in result["items"] if g["title"] == "Hello")
    assert len(hello["versions"]) == 3
    assert {v["version"] for v in hello["versions"]} == {"未标注", "Live", "伴奏"}
    assert all(v["format"] == "WAV" for v in hello["versions"])
    assert radar(library, q="Remastered")["total"] == 1
    assert len(radar(library, page=2, size=1)["items"]) == 1
    with pytest.raises(ValueError):
        radar(library, page=0)
    library.save([], False)
    assert radar(library)["total"] == 0
