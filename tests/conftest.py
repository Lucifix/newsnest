import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated config + data folders seeded from config.example/."""
    cfg_dir, data_dir = tmp_path / "config", tmp_path / "data"
    shutil.copytree(ROOT / "config.example", cfg_dir)
    data_dir.mkdir()
    monkeypatch.setenv("NEWSNEST_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("NEWSNEST_DATA_DIR", str(data_dir))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return SimpleNamespace(config=cfg_dir, data=data_dir)


@pytest.fixture
def conn(env):
    from newsnest.db import connect, migrate
    c = connect()
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def cfg(env):
    from newsnest.config import load_config
    loaded = load_config(env.config)
    assert not loaded.errors, loaded.errors
    loaded.config.scoring.min_batch = 1  # score at once; the batching wait has its own tests
    return loaded.config


class FakeMessages:
    """Stands in for client.messages; `handler(kwargs)` returns (text, stop_reason)."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text, stop = self.handler(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
            stop_details=SimpleNamespace(category="general_harms") if stop == "refusal" else None,
            usage=SimpleNamespace(input_tokens=1000, output_tokens=500,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=2000))


def fake_client(handler):
    return SimpleNamespace(messages=FakeMessages(handler))


def echo_scores(kwargs, topic=7, importance=5, personal=6):
    """Scores every id found in the items block."""
    import re
    text = kwargs["messages"][0]["content"][-1]["text"]
    ids = [int(i) for i in re.findall(r"id=(\d+)", text)]
    return json.dumps({"items": [{"id": i, "topic": topic, "importance": importance, "personal": personal,
                                  "reason": "test", "category": "Local"} for i in ids]}), "end_turn"


@pytest.fixture(autouse=True)
def logged_in(monkeypatch, request):
    """Tests act as a logged-in user, except tests marked with @pytest.mark.auth."""
    if "auth" not in request.keywords:
        import newsnest.web
        monkeypatch.setattr(newsnest.web, "request_authorized", lambda req: True)
