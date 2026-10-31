"""interests.md as the two bullet lists it really is: "## Want" and "## Skip".

Changes are applied line by line to the real file, so bullets nobody touched, comments and any other
text stay exactly as they were.
"""

from __future__ import annotations

import difflib
import re

SECTIONS = ("want", "skip")
OPS = ("add", "remove", "reword")


def norm(text: str) -> str:
    """Bullets compare ignoring case, spacing and a final full stop."""
    return re.sub(r"\s+", " ", text).strip().rstrip(".").casefold()


def _section_of(heading: str) -> str | None:
    heading = heading.casefold()
    return next((s for s in SECTIONS if heading.startswith(s)), None)


def _bullet(line: str) -> str | None:
    m = re.match(r"^\s*[-*]\s+(.*\S)\s*$", line)
    return m.group(1) if m else None


def _scan(lines: list[str]) -> list[tuple[str | None, str | None, bool]]:
    """Per line: (section, bullet text, is a section heading). HTML comments and other sections give (None, None, False)."""
    out, section, in_comment = [], None, False
    for line in lines:
        if in_comment:
            in_comment = "-->" not in line
            out.append((None, None, False))
            continue
        if "<!--" in line:
            in_comment = "-->" not in line.split("<!--", 1)[1]
            out.append((None, None, False))
            continue
        m = re.match(r"^##\s+(.*?)\s*$", line)
        if m:
            section = _section_of(m.group(1))
            out.append((section, None, True))
        else:
            out.append((section, _bullet(line) if section else None, False))
    return out


def bullets(text: str) -> dict[str, list[str]]:
    """The bullets under Want and Skip."""
    out: dict[str, list[str]] = {s: [] for s in SECTIONS}
    for section, bullet, _ in _scan(text.split("\n")):
        if section and bullet:
            out[section].append(bullet)
    return out


def apply_changes(text: str, changes: list[dict]) -> tuple[str, list[dict], list[tuple[dict, str]]]:
    """Apply add / remove / reword changes to the file text.
    Returns (new text, changes applied, [(change, why it was skipped)])."""
    lines = text.split("\n")
    applied, skipped = [], []
    for change in changes:
        op, section = change.get("op"), change.get("section")
        old, new = (change.get("old") or "").strip(), (change.get("new") or "").strip()
        if op not in OPS or section not in SECTIONS:
            skipped.append((change, "not a valid change"))
            continue
        scan = _scan(lines)
        at = next((i for i, (s, b, _) in enumerate(scan) if s == section and b and norm(b) == norm(old)), None) if old else None
        if op == "add":
            if not new:
                skipped.append((change, "nothing to add"))
            elif any(s == section and b and norm(b) == norm(new) for s, b, _ in scan):
                skipped.append((change, "already in your interests"))
            else:
                head = next((i for i, (s, _, h) in enumerate(scan) if s == section and h), None)
                if head is None:  # no such section yet: start one at the end
                    while lines and not lines[-1].strip():
                        lines.pop()
                    lines += ["", f"## {section.capitalize()}", f"- {new}", ""]
                else:
                    last = max([i for i, (s, b, _) in enumerate(scan) if s == section and b] or [head])
                    lines.insert(last + 1, f"- {new}")
                applied.append(change)
        elif at is None:
            skipped.append((change, "that bullet is no longer in your interests"))
        elif op == "remove":
            del lines[at]
            applied.append(change)
        elif not new or norm(new) == norm(old):
            skipped.append((change, "nothing to reword"))
        else:
            lines[at] = f"- {new}"
            applied.append(change)
    return "\n".join(lines), applied, skipped


def bullet_diff(current: str, proposed: str) -> dict[str, list[tuple[str, str]]]:
    """Bullets only in one of the two texts, as (section, text): what a whole-file proposal really changes."""
    a, b = bullets(current), bullets(proposed)
    seen_a = {(s, norm(t)) for s in SECTIONS for t in a[s]}
    seen_b = {(s, norm(t)) for s in SECTIONS for t in b[s]}
    return {"removed": [(s, t) for s in SECTIONS for t in a[s] if (s, norm(t)) not in seen_b],
            "added": [(s, t) for s in SECTIONS for t in b[s] if (s, norm(t)) not in seen_a]}


def word_diff(old: str, new: str) -> list[tuple[str, str]]:
    """[(kind, text)] with kind 'same', 'del' or 'ins', by words: shows what a reword changes."""
    a, b = old.split(), new.split()
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            out.append(("same", " ".join(a[i1:i2])))
        else:
            if i2 > i1:
                out.append(("del", " ".join(a[i1:i2])))
            if j2 > j1:
                out.append(("ins", " ".join(b[j1:j2])))
    return out
