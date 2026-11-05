import asyncio
from pathlib import Path
import sys
import tempfile
import httpx
import pytest


@pytest.mark.asyncio
async def test_listener_restart_after_crash_only_removes_owned_socket():
    with tempfile.TemporaryDirectory(prefix="fnlx-cli-", dir="/tmp") as folder:
        root = Path(folder)
        sock = root / "p.sock"
        env = root / "config.env"
        env.write_text(
            f"LX_PASSWORD=fixture-password-not-a-secret\nDATA_DIR={root}/state\nDOWNLOAD_DIR={root}/music\nLISTEN_SOCKET={sock}\nUPSTREAM_SOCKET={root}/original.sock\n"
        )

        async def launch():
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "fnmusic",
                "--env",
                str(env),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            for _ in range(100):
                if process.returncode is not None:
                    pytest.fail("Listener exited before becoming ready")
                if sock.exists():
                    try:
                        async with httpx.AsyncClient(
                            transport=httpx.AsyncHTTPTransport(uds=str(sock)),
                            base_url="http://localhost",
                            timeout=1,
                        ) as client:
                            if (await client.get("/_ext/healthz")).status_code == 200:
                                return process
                    except httpx.HTTPError:
                        pass
                await asyncio.sleep(0.05)
            process.kill()
            await process.wait()
            pytest.fail("Listener startup timed out")

        first = await launch()
        first.kill()
        await first.wait()
        assert sock.exists()
        second = await launch()
        try:
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=str(sock)),
                base_url="http://localhost",
            ) as client:
                assert (await client.get("/_ext/jobs")).status_code == 401
        finally:
            second.terminate()
            await asyncio.wait_for(second.wait(), 10)
        assert not sock.exists()
        assert not (root / "music").exists()
