"""Container entrypoint: web UI + in-process scheduler, structured JSON logs on stdout."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import uvicorn

from newsnest.bootstrap import ensure_dirs, env

# Attributes every LogRecord has; anything else came in via extra={...} and is logged as a field
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
                 "level": record.levelname.lower(), "logger": record.name, "msg": record.getMessage()}
        entry.update({k: v for k, v in vars(record).items() if k not in _STANDARD})
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel((env("LOG_LEVEL", "INFO") or "INFO").upper())
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default", "trafilatura"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    setup_logging()
    ensure_dirs()
    uvicorn.run("newsnest.web:app", host="0.0.0.0", port=int(env("PORT", "8080")),
                workers=1, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
