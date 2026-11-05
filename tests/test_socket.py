import socket
import tempfile
from pathlib import Path

import pytest

from scripts.socket_control import restore, take_over


@pytest.fixture
def tmp_path():
    with tempfile.TemporaryDirectory(prefix="fnlx-", dir="/tmp") as folder:
        yield Path(folder)


def listener(path):
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(path))
    s.listen()
    return s


def test_socket_takeover_restore_and_idempotence(tmp_path):
    public = tmp_path / "official.sock"
    backup = tmp_path / "original.sock"
    proxy = tmp_path / "proxy.sock"
    state = tmp_path / "state.json"
    a = listener(public)
    b = listener(proxy)
    try:
        inode = public.stat().st_ino
        take_over(public, backup, proxy, state)
        assert public.is_symlink() and public.resolve() == proxy.resolve()
        assert backup.stat().st_ino == inode
        take_over(public, backup, proxy, state)
        restore(public, backup, proxy, state)
        assert not public.is_symlink() and public.stat().st_ino == inode
        restore(public, backup, proxy, state)
    finally:
        a.close()
        b.close()


def test_unknown_socket_never_overwritten(tmp_path):
    public = tmp_path / "o.sock"
    backup = tmp_path / "b.sock"
    proxy = tmp_path / "p.sock"
    state = tmp_path / "s.json"
    a = listener(public)
    b = listener(proxy)
    c = listener(backup)
    try:
        with pytest.raises(RuntimeError):
            take_over(public, backup, proxy, state)
        assert not public.is_symlink()
    finally:
        a.close()
        b.close()
        c.close()


def test_official_restart_does_not_destroy_new_socket(tmp_path):
    public = tmp_path / "o.sock"
    backup = tmp_path / "b.sock"
    proxy = tmp_path / "p.sock"
    state = tmp_path / "s.json"
    a = listener(public)
    b = listener(proxy)
    try:
        take_over(public, backup, proxy, state)
        public.unlink()
        c = listener(public)
        try:
            inode = public.stat().st_ino
            with pytest.raises(RuntimeError):
                restore(public, backup, proxy, state)
            assert public.stat().st_ino == inode and backup.exists()
        finally:
            c.close()
    finally:
        a.close()
        b.close()
