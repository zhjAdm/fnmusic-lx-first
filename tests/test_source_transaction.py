from pathlib import Path
import httpx
import pytest
from fnmusic.lx import LX
from fnmusic.control import validate_directory
from fnmusic.control import Settings, atomic_text
from fnmusic.config import Config
import json


@pytest.mark.asyncio
async def test_failed_source_toggle_is_rejected_and_deleted(tmp_path):
    script = tmp_path / "source.js"
    script.write_text("const lx=globalThis.lx;")
    calls = []

    def handle(req):
        calls.append(req.url.path)
        if req.url.path.endswith("/upload"):
            return httpx.Response(200, json={"success": True, "id": "same-id"})
        if req.url.path.endswith("/toggle"):
            return httpx.Response(200, json={"success": False, "disabledVM": True})
        if req.url.path.endswith("/delete"):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        base_url="http://lx", transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(ValueError):
            await LX(client, "private", ["kw"]).import_script(script)
    assert calls[-1].endswith("/delete")


def test_nonexistent_volume_cannot_be_created(monkeypatch):
    def no_mkdir(*args, **kwargs):
        raise AssertionError("Must not create a fake volume")

    monkeypatch.setattr(Path, "mkdir", no_mkdir)
    with pytest.raises(ValueError):
        validate_directory("/vol999999/not-a-real-volume")


@pytest.mark.asyncio
async def test_failed_same_name_update_restores_old_script_and_takeover(tmp_path):
    old = "/*! @name Same source */\nconst lx=globalThis.lx; // OLD"
    new = "/*! @name Same source */\nconst lx=globalThis.lx; // BAD"
    loaded = {"same": old}

    def handle(req):
        body = json.loads(req.content) if req.content else {}
        if req.url.path.endswith("/delete"):
            loaded.pop(body["id"], None)
            return httpx.Response(200, json={"success": True})
        if req.url.path.endswith("/upload"):
            if "same" in loaded:
                return httpx.Response(500, json={"error": "duplicate"})
            loaded["same"] = body["content"]
            return httpx.Response(200, json={"success": True, "id": "same"})
        if req.url.path.endswith("/toggle"):
            ok = "BAD" not in loaded["same"]
            return httpx.Response(200, json={"success": ok, "enabled": ok})
        return httpx.Response(
            200,
            json=[
                {
                    "id": "same",
                    "enabled": True,
                    "status": "success",
                    "sources": {"kw": {"qualitys": ["320k"]}},
                }
            ],
        )

    class Control:
        active = True

        async def running(self):
            return self.active

        async def disable(self):
            self.active = False

        async def enable(self):
            self.active = True

    control = Control()
    env = tmp_path / "worker.env"
    env.write_text("LX_PASSWORD=fixture-private\n")
    async with httpx.AsyncClient(
        base_url="http://lx", transport=httpx.MockTransport(handle)
    ) as client:
        settings = Settings(
            Config(data_dir=tmp_path, platforms=["kw"]),
            env,
            control,
            LX(client, "fixture-private", ["kw"]),
        )
        atomic_text(settings.source_path, old)
        settings.write(
            {
                "source_id": "same",
                "source_name": "Old",
                "source_version": "1",
                "download_dir": "/vol1/music",
                "takeover": True,
                "play_mode": "direct",
            }
        )
        with pytest.raises(ValueError):
            await settings.upload("source.js", new)
        assert loaded["same"] == old
        assert settings.source_path.read_text() == old
        assert settings.read()["source_version"] == "1"
        assert control.active
