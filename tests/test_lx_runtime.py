"""Real pinned runtime contract test; uses an artificial source, never the paid source."""

import asyncio
import os
import shutil
import socket
import subprocess
from pathlib import Path

import httpx
import pytest

from fnmusic.lx import LX
from scripts.runtime import configure


@pytest.mark.asyncio
async def test_pinned_runtime_import_and_registered_qualities(tmp_path):
    runtime = Path("data/runtime/lxserver").resolve()
    if not (runtime / "index.js").exists() or not shutil.which("node"):
        pytest.skip("Run scripts.runtime prepare to install the pinned LX test runtime")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    password = "fixture-password-not-a-secret"
    config = tmp_path / "config.js"
    configure(config, password, port)
    env = dict(
        os.environ,
        DATA_PATH=str(tmp_path / "state"),
        CONFIG_PATH=str(config),
        PORT=str(port),
        BIND_IP="127.0.0.1",
    )
    script = tmp_path / "fixture.js"
    script.write_text("""/*! @name Integration fixture */
const {on,send,EVENT_NAMES}=globalThis.lx;
on(EVENT_NAMES.request, () => Promise.resolve('https://127.0.0.1:9/fixture.flac'));
send(EVENT_NAMES.inited,{status:true,sources:{kw:{name:'fixture',type:'music',actions:['musicUrl'],qualitys:['128k','flac']}}});
""")
    with (tmp_path / "node.log").open("wb") as logs:
        process = subprocess.Popen(
            ["node", "index.js"],
            cwd=runtime,
            env=env,
            stdout=logs,
            stderr=subprocess.STDOUT,
        )
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False
            ) as client:
                lx = LX(client, password, ["kw"])
                for _ in range(100):
                    if process.poll() is not None:
                        pytest.fail("LX runtime exited; inspect temporary node.log")
                    try:
                        response = await client.get("/api/status")
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("LX runtime did not become ready")
                await lx.import_script(script)
                for _ in range(40):
                    if await lx.qualities("kw") == ["flac", "128k"]:
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("Registered LX quality contract mismatch")
                result = await lx.resolve(
                    {
                        "source": "kw",
                        "songmid": "42",
                        "name": "Fixture",
                        "singer": "Test",
                    },
                    "flac",
                )
                assert "fixture.flac" in result["url"]
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
