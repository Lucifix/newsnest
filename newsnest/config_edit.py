"""Edits to config.yaml from the Settings page, keeping every comment and the file's layout.

Each edit is validated with the same schema the app loads with before anything is written;
the previous file is kept as config.yaml.bak.
"""

from __future__ import annotations

import io
import json
import os
import shutil
from pathlib import Path
from typing import Callable

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from newsnest.bootstrap import config_dir
from newsnest.config import (KNOWN_SECTIONS, LEGACY_SECTIONS, MAX_TABS, NEWS, RESERVED_KEYS, WORLD, Config, SectionCfg,
                             build_sources, drop_removed, slugify)


class ConfigEditError(Exception):
    pass


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    y.representer.add_representer(type(None), lambda r, _: r.represent_scalar("tag:yaml.org,2002:null", "null"))
    return y


def config_path() -> Path:
    return config_dir() / "config.yaml"


def edit_config(change: Callable[[CommentedMap], None]) -> None:
    """Load config.yaml round-trip, apply `change`, validate, write atomically."""
    y = _yaml()
    path = config_path()
    data = y.load(path.read_text(encoding="utf-8"))
    change(data)
    plain = json.loads(json.dumps(data, default=str))
    # Validate the way load_config reads it: options from older versions are ignored, not errors. They
    # stay in the file untouched.
    drop_removed(plain)
    try:
        cfg = Config.model_validate(plain)
        build_sources(cfg)
    except Exception as e:  # pydantic ValidationError or duplicate ids
        raise ConfigEditError(str(e)) from e
    out = io.StringIO()
    y.dump(data, out)
    shutil.copy2(path, path.with_suffix(".yaml.bak"))
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(out.getvalue(), encoding="utf-8")
    os.replace(tmp, path)


def section_list(data: CommentedMap, *keys: str) -> list:
    """The list at data[k1][k2]..., created if missing."""
    node = data
    for k in keys[:-1]:
        if node.get(k) is None:
            node[k] = CommentedMap()
        node = node[k]
    if node.get(keys[-1]) is None:
        node[keys[-1]] = []
    return node[keys[-1]]


def section_map(data: CommentedMap, key: str) -> CommentedMap:
    """The mapping at data[key], created (block style, at the end of the file) if missing."""
    if data.get(key) is None:
        data[key] = CommentedMap()
    return data[key]


SECTIONS_COMMENT = (
    "Sections: the tabs and chips of the feed. Local (news) and World are built in: rename them, they can't go.\n"
    "key: fixed id stored with every story; label: the name shown; icon: chip | watch | ball | flask | briefcase |\n"
    "film | leaf | star; tab: false makes it a chip under For you instead of a tab (at most 4 tabs, News is one).\n"
    "Manage them in Settings → Feeds; a feed's category: is one of these keys.")


def section_entry(key: str, label: str, icon: str = "star", tab: bool = True) -> CommentedMap:
    """One `sections:` item, written on a single line like {key: tech, label: Tech, icon: chip}."""
    entry = CommentedMap()
    entry["key"], entry["label"] = key, label
    if icon != "star":
        entry["icon"] = icon
    if not tab:
        entry["tab"] = False
    entry.fa.set_flow_style()
    return entry


def materialize_sections(data: CommentedMap) -> CommentedSeq:
    """The `sections:` list, written into the file (with the sections every older config implied) when the
    config didn't have one yet. Placed before schedules:, so the comments of the feeds below stay with them."""
    if data.get("sections") is not None:
        return data["sections"]
    seq = CommentedSeq(section_entry(s.key, s.label, s.icon, s.tab) for s in LEGACY_SECTIONS)
    keys = list(data.keys())
    before = next((k for k in ("schedules", "feeds") if k in keys), None)
    data.insert(keys.index(before) if before else len(keys), "sections", seq)
    data.yaml_set_comment_before_after_key("sections", before=SECTIONS_COMMENT)
    return seq


def tab_room(seq: list) -> bool:
    """Whether one more section can be a tab (News is one, and it counts)."""
    return 2 + sum(1 for s in seq if s.get("key") not in (NEWS, WORLD) and s.get("tab", True)) <= MAX_TABS


def free_section_key(seq: list, label: str) -> str:
    """A key for a new section: its name as a slug, numbered if taken (keys are never reused or renamed)."""
    taken = {s.get("key") for s in seq} | set(RESERVED_KEYS)
    key = base = slugify(label, 20)
    n = 2
    while key in taken:
        key, n = f"{base}-{n}", n + 1
    return key


def ensure_section(data: CommentedMap, key: str) -> None:
    """A pack files feeds under a section the reader's list has dropped (Tech, Lifestyle): bring it back.
    Nothing to do while the config has no `sections:` list, which implies all four."""
    known: SectionCfg | None = KNOWN_SECTIONS.get(key)
    seq = data.get("sections")
    if seq is None or known is None or any(s.get("key") == key for s in seq):
        return
    seq.append(section_entry(key, known.label, known.icon, tab=tab_room(seq)))


def remove_where(items: list, predicate: Callable[[dict], bool]) -> int:
    before = len(items)
    for i in range(len(items) - 1, -1, -1):
        if predicate(items[i]):
            del items[i]
    return before - len(items)
