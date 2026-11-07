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
    # The path is built from our own constant, never from the name that was passed in
    try:
        known = PROFILE_FILES[PROFILE_FILES.index(name)]
    except ValueError:
        raise ValueError(f"not a profile file: {name}") from None
    return config_dir() / known


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
    """Drop every <!-- ... --> comment. An unclosed <!-- and everything after it is kept.

    A plain loop, not re.sub(r"<!--.*?-->"): that regex got very slow on text with many unclosed <!--
    (CodeQL py/polynomial-redos)."""
    out, pos = [], 0
    while (start := text.find("<!--", pos)) != -1:
        end = text.find("-->", start + 4)
        if end == -1:
            break
        out.append(text[pos:start])
        pos = end + 3
    out.append(text[pos:])
    return "".join(out).strip()


def seed_examples() -> list[tuple[bool, str]]:
    """Lines starting with + (want) or - (don't want)."""
    out = []
    for line in strip_comments(read_profile("seed_examples.md")).splitlines():
        line = line.strip()
        if line[:1] in "+-" and len(line) > 1:
            out.append((line[0] == "+", line[1:].strip()))
    return out
