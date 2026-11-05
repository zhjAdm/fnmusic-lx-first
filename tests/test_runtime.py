from scripts.runtime import expose_capabilities


def test_capability_patch_is_minimal_and_idempotent(tmp_path):
    target = tmp_path / "server/server"
    target.mkdir(parents=True)
    path = target / "customSourceHandlers.js"
    path.write_text(
        "        const status = (0, userApi_1.getApiStatus)(source.owner, source.id);\n"
    )
    expose_capabilities(tmp_path)
    first = path.read_text()
    assert "getLoadedApis" in first and "qualitys" in first
    assert "script:" not in first
    expose_capabilities(tmp_path)
    assert path.read_text() == first
