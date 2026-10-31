"""First-start setup: seed an empty config folder from config.example/ and check folders are writable."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_DIR = REPO_ROOT / "config.example"


_warned_legacy: set[str] = set()


def env(name: str, default: str | None = None) -> str | None:
    """NEWSNEST_<name>. Installs that predate the rename set RADAR_<name>; it is still honoured."""
    value = os.environ.get(f"NEWSNEST_{name}")
    if value is not None:
        return value
    value = os.environ.get(f"RADAR_{name}")
    if value is None:
        return default
    if name not in _warned_legacy:
        _warned_legacy.add(name)
        log.warning("RADAR_%s is the old name of this setting: rename it to NEWSNEST_%s", name, name)
    return value


def config_dir() -> Path:
    return Path(env("CONFIG_DIR", str(REPO_ROOT / "config")))


def data_dir() -> Path:
    return Path(env("DATA_DIR", str(REPO_ROOT / "data")))


def adopt_legacy_database(data: Path) -> bool:
    """Databases created before the rename are called radar.db; move one (with its WAL files) to newsnest.db."""
    old, new = data / "radar.db", data / "newsnest.db"
    if new.exists() or not old.exists():
        return False
    for suffix in ("", "-wal", "-shm"):
        if (data / f"radar.db{suffix}").exists():
            (data / f"radar.db{suffix}").rename(data / f"newsnest.db{suffix}")
    log.info("renamed %s to %s", old, new)
    return True


def ensure_dirs() -> None:
    cfg, data = config_dir(), data_dir()
    for d in (cfg, data):
        d.mkdir(parents=True, exist_ok=True)
        if not os.access(d, os.W_OK):
            # Bind-mounted host folders created by Docker are owned by root
            sys.exit(f"{d} is not writable by uid {os.getuid()}: chown it on the host "
                     f"(see the README, 'Quick start')")
    adopt_legacy_database(data)
    if not any(cfg.iterdir()):
        shutil.copytree(EXAMPLE_DIR, cfg, dirs_exist_ok=True)
        log.info("config folder was empty: copied %s into %s", EXAMPLE_DIR, cfg)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure_dirs()
