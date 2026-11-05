"""Reversible Unix Socket takeover with ownership checks and a shared lock."""

import argparse
import fcntl
import json
import os
import socket
import stat
from contextlib import contextmanager
from pathlib import Path


def identity(path):
    st = path.stat()
    if not stat.S_ISSOCK(st.st_mode):
        raise RuntimeError("Expected a Unix socket")
    return [st.st_dev, st.st_ino]


@contextmanager
def locked(public):
    with (public.parent / ("." + public.name + ".fnmusic.lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def read_state(state, public, backup, proxy):
    data = json.loads(state.read_text())
    if [data.get(k) for k in ("public", "backup", "proxy")] != [
        str(x.absolute()) for x in (public, backup, proxy)
    ]:
        raise RuntimeError("Takeover belongs to another deployment")
    return data


def owned_link(public, proxy):
    return public.is_symlink() and os.readlink(public) == str(proxy.absolute())


def take_over(public, backup, proxy, state):
    public, backup, proxy, state = map(Path, (public, backup, proxy, state))
    if len({str(p.absolute()) for p in (public, backup, proxy, state)}) != 4:
        raise RuntimeError("Paths must differ")
    with locked(public):
        if state.exists():
            data = read_state(state, public, backup, proxy)
            if (
                owned_link(public, proxy)
                and identity(backup) == data["original"]
                and identity(proxy) == data["proxy_identity"]
            ):
                return
            raise RuntimeError(
                "Incomplete or changed takeover; restore and inspect before retrying"
            )
        if public.is_symlink() or backup.exists() or backup.is_symlink():
            raise RuntimeError("Socket already managed or backup occupied")
        original, proxy_id = identity(public), identity(proxy)
        data = {
            "public": str(public.absolute()),
            "backup": str(backup.absolute()),
            "proxy": str(proxy.absolute()),
            "original": original,
            "proxy_identity": proxy_id,
        }
        state.parent.mkdir(parents=True, exist_ok=True)
        with state.open("x") as f:
            os.chmod(state, 0o600)
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        try:
            public.rename(backup)
            public.symlink_to(proxy.absolute())
        except BaseException:
            if not public.exists() and not public.is_symlink() and backup.exists():
                backup.rename(public)
            if (
                public.exists()
                and not public.is_symlink()
                and identity(public) == original
            ):
                state.unlink(missing_ok=True)
            raise


def restore(public, backup, proxy, state):
    public, backup, proxy, state = map(Path, (public, backup, proxy, state))
    with locked(public):
        if not state.exists():
            if public.is_symlink() or backup.exists():
                raise RuntimeError("No ownership journal; preserving sockets")
            identity(public)
            return
        data = read_state(state, public, backup, proxy)
        if not backup.exists():
            if (
                public.exists()
                and not public.is_symlink()
                and identity(public) == data["original"]
            ):
                state.unlink()
                return
            raise RuntimeError("Original socket missing; preserving current endpoint")
        if identity(backup) != data["original"]:
            raise RuntimeError("Original socket changed")
        if public.exists() or public.is_symlink():
            if not owned_link(public, proxy):
                raise RuntimeError("Official endpoint changed; preserving both sockets")
            public.unlink()
        backup.rename(public)
        state.unlink()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["enable", "restore"])
    p.add_argument("--public", default="/var/run/trim_music.socket")
    p.add_argument("--backup", default="/var/run/trim_music.original.socket")
    p.add_argument("--proxy", default="/var/run/fnmusic-lx.socket")
    p.add_argument("--state", default="data/takeover.json")
    args = p.parse_args()
    if args.action == "enable":
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(3)
            conn.connect(args.proxy)
            conn.sendall(b"GET /_ext/healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")
            if b"200 OK" not in conn.recv(4096):
                raise RuntimeError("Proxy health check failed")
    fn = take_over if args.action == "enable" else restore
    fn(args.public, args.backup, args.proxy, args.state)
    print("Socket operation complete")


if __name__ == "__main__":
    main()
