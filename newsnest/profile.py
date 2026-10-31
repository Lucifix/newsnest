"""The profile files in config/: interests.md, seed_examples.md.

They start empty (explanatory HTML comments only) and are edited on the Profile page.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from newsnest.bootstrap import config_dir
from newsnest.config import is_blank_markdown

PROFILE_FILES = ("interests.md", "seed_examples.md")


def profile_path(name: str) -> Path:
    if name not in PROFILE_FILES:
        raise ValueError(f"not a profile file: {name}")
    return config_dir() / name


def read_profile(name: str) -> str:
    try:
        return profile_path(name).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_profile(name: str, text: str) -> None:
    """Atomic write with a .bak of the previous version."""
    path = profile_path(name)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
    os.replace(tmp, path)


def profile_exists() -> bool:
    return not is_blank_markdown(read_profile("interests.md"))


def strip_comments(text: str) -> str:
    import re
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


def seed_examples() -> list[tuple[bool, str]]:
    """Lines starting with + (want) or - (don't want)."""
    out = []
    for line in strip_comments(read_profile("seed_examples.md")).splitlines():
        line = line.strip()
        if line[:1] in "+-" and len(line) > 1:
            out.append((line[0] == "+", line[1:].strip()))
    return out
