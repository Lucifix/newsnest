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
from ruamel.yaml.comments import CommentedMap

from newsnest.bootstrap import config_dir
from newsnest.config import Config, build_sources, drop_removed


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


def remove_where(items: list, predicate: Callable[[dict], bool]) -> int:
    before = len(items)
    for i in range(len(items) - 1, -1, -1):
        if predicate(items[i]):
            del items[i]
    return before - len(items)
