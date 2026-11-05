import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path


def load_env(path):
    """Parse KEY=value with quoted values; never execute or expand shell expressions."""
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            raise ValueError(f"Invalid environment key at line {number}")
        tokens = shlex.split(value, comments=True)
        if len(tokens) > 1:
            raise ValueError(f"Quote environment value at line {number}")
        os.environ.setdefault(key.strip(), tokens[0] if tokens else "")


@dataclass
class Config:
    data_dir: Path = Path("data")
    install_root: Path | None = None
    download_dir: Path = Path("data/music")
    upstream_socket: str = "/var/run/trim_music.original.socket"
    listen_socket: str = "/var/run/fnmusic-lx.socket"
    lx_url: str = "http://127.0.0.1:8005"
    lx_password: str = ""
    platforms: list[str] = field(default_factory=lambda: ["kg", "tx", "kw", "wy", "mg"])
    play_mode: str = "direct"
    play_quality: str = "320k"
    playlist_name: str = "默认歌单"
    default_playlist_guid: str = ""
    scan_path: str = ""
    scan_body: dict = field(default_factory=dict)
    public_playlists: list[dict] = field(default_factory=list)
    hot_source: str = "kg"
    hot_board: str = ""
    max_download_bytes: int = 1024 * 1024 * 1024
    session_ttl: int = 3600

    def __post_init__(self):
        self.data_dir, self.download_dir = Path(self.data_dir), Path(self.download_dir)
        if self.play_mode not in ("direct", "proxy"):
            raise ValueError("PLAY_MODE must be direct or proxy")
        if not self.platforms or any(
            p not in {"kw", "kg", "tx", "wy", "mg"} for p in self.platforms
        ):
            raise ValueError("Unsupported LX platform")
        if self.scan_path and not self.scan_path.startswith("/music/api/v1/"):
            raise ValueError("Scan path must be an official music API path")
        for row in self.public_playlists:
            if (
                not isinstance(row, dict)
                or row.get("source") not in self.platforms
                or not row.get("id")
            ):
                raise ValueError("Invalid public playlist configuration")
        if self.play_quality not in {
            "128k",
            "320k",
            "flac",
            "flac24bit",
            "hires",
            "master",
            "atmos",
            "atmos_plus",
        }:
            raise ValueError("Invalid playback quality")
        if (
            self.listen_socket == self.upstream_socket
            or self.listen_socket == "/var/run/trim_music.socket"
        ):
            raise ValueError("Listener must not overwrite the official socket")

    @classmethod
    def from_env(cls):
        env = os.environ
        config = cls(
            install_root=Path(env["APP_ROOT"]) if env.get("APP_ROOT") else None,
            data_dir=Path(env.get("DATA_DIR", "data")),
            download_dir=Path(env.get("DOWNLOAD_DIR", "data/music")),
            upstream_socket=env.get("UPSTREAM_SOCKET", cls.upstream_socket),
            listen_socket=env.get("LISTEN_SOCKET", cls.listen_socket),
            lx_url=env.get("LX_URL", cls.lx_url),
            lx_password=env.get("LX_PASSWORD", ""),
            platforms=env.get("LX_PLATFORMS", "kg,tx,kw,wy,mg").split(","),
            play_mode=env.get("PLAY_MODE", "direct"),
            play_quality=env.get("PLAY_QUALITY", "320k"),
            default_playlist_guid=env.get("DEFAULT_PLAYLIST_GUID", ""),
            scan_path=env.get("SCAN_PATH", ""),
            scan_body=json.loads(env.get("SCAN_BODY", "{}")),
            public_playlists=json.loads(env.get("PUBLIC_PLAYLISTS", "[]")),
            hot_source=env.get("HOT_SOURCE", "kg"),
            hot_board=env.get("HOT_BOARD", ""),
        )
        if not config.lx_password:
            raise ValueError("LX_PASSWORD is required")
        return config
