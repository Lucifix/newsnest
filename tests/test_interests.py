import json
from datetime import datetime, timezone

from newsnest.ai import AIClient
from newsnest.db import iso
from newsnest.interests import apply_changes, bullet_diff, bullets, word_diff
from newsnest.profile import read_profile, write_profile
from newsnest.profile_ai import weekly_proposal
from tests.conftest import fake_client

NOW = datetime.now(timezone.utc)
FILE = "<!--\nnotes\n- not a bullet\n-->\n## Want\n- Linux and homelab\n- AI coding tools\n\n## Skip\n- Sports.\n"


def change(op, section, old="", new="", reason="because"):
    return {"op": op, "section": section, "old": old, "new": new, "reason": reason}


def test_bullets_ignore_comments_and_other_text():
    assert bullets(FILE) == {"want": ["Linux and homelab", "AI coding tools"], "skip": ["Sports."]}


def test_apply_changes_touches_only_what_it_names():
    new, applied, skipped = apply_changes(FILE, [
        change("add", "want", new="Store recalls"),
        change("reword", "want", old="linux  and homelab", new="Linux, homelab and NAS"),  # spacing and case don't matter
        change("remove", "skip", old="Sports"),  # the final full stop doesn't matter either
        change("add", "skip", new="Club sports")])
    assert not skipped and len(applied) == 4
    assert new == ("<!--\nnotes\n- not a bullet\n-->\n## Want\n- Linux, homelab and NAS\n- AI coding tools\n- Store recalls\n\n"
                   "## Skip\n- Club sports\n")


def test_apply_changes_skips_what_no_longer_fits():
    new, applied, skipped = apply_changes(FILE, [
        change("remove", "want", old="Gone since you edited the file"), change("add", "want", new="ai coding tools"),
        change("reword", "want", old="AI coding tools", new="AI coding tools"), change("add", "elsewhere", new="x")])
    assert new == FILE and applied == []
    assert [why for _, why in skipped] == ["that bullet is no longer in your interests", "already in your interests",
                                          "nothing to reword", "not a valid change"]


def test_apply_changes_starts_a_missing_section():
    new, applied, _ = apply_changes("## Want\n- A\n", [change("add", "skip", new="Gossip")])
    assert len(applied) == 1 and bullets(new) == {"want": ["A"], "skip": ["Gossip"]}


def test_bullet_diff_and_word_diff():
    assert bullet_diff(FILE, "## Want\n- linux and homelab\n- Recalls\n## Skip\n- Sports\n") == {
        "removed": [("want", "AI coding tools")], "added": [("want", "Recalls")]}
    assert word_diff("AI coding agents", "AI coding agents and review") == [("same", "AI coding agents"), ("ins", "and review")]


def proposal_ai(cfg, conn, changes, summary="Short summary"):
    return AIClient(cfg, conn, fake_client(lambda kw: (json.dumps({"summary": summary, "changes": changes}), "end_turn")))


def test_weekly_proposal_stores_applicable_changes(conn, cfg, env):
    write_profile("interests.md", FILE)
    ai = proposal_ai(cfg, conn, [change("add", "want", new="Store recalls", reason="rated up a recall"),
                                 change("remove", "want", old="a bullet that is not there")])
    pid = weekly_proposal(conn, cfg, ai)
    row = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
    stored = json.loads(row["changes"])
    assert row["status"] == "pending" and row["rationale"] == "Short summary"
    assert [c["new"] for c in stored["items"]] == ["Store recalls"]  # the misquoted remove was dropped
    assert stored["basis"]["ratings"] == 0 and "- Store recalls" in row["proposed_text"]
    assert read_profile("interests.md") == FILE  # nothing is written until the changes are accepted
    system = ai.client.messages.calls[0]["system"][0]["text"]
    assert "smallest set of changes" in system and "individual headlines" in system
    # no applicable change at all: nothing for the reader to decide
    pid = weekly_proposal(conn, cfg, proposal_ai(cfg, conn, []))
    assert conn.execute("SELECT status FROM proposals WHERE id = ?", (pid,)).fetchone()[0] == "no_change"
