# Contributing to NewsNest

Bug reports, ideas and pull requests are welcome. Found a bug? Settings → *About & report a bug* in the
app opens a new issue with your version filled in. Please open an issue first for anything big. Planned
work is tracked in [GitHub issues](https://github.com/Lucifix/newsnest/issues).

## License of your contribution

NewsNest is licensed under the [GNU AGPL v3.0 or later](LICENSE). By opening a pull request you agree
that your contribution is released under that license, and that the maintainer may also release it under
other licenses later (for example a commercial license next to the AGPL).

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest ruff==0.17.3
.venv/bin/python -m pytest                 # run the tests
.venv/bin/ruff check .                     # the linter CI runs on every PR
NEWSNEST_PASSWORD=dev NEWSNEST_SCHEDULER=off .venv/bin/python -m newsnest   # the app on :8080
```

`NEWSNEST_SCHEDULER=off` stops background fetching, so the app only shows what is already in the database.

The first run copies `config.example/` to `./config/` (git-ignored). Two helpers are useful when
adding or debugging sources:

```bash
.venv/bin/python -m newsnest.spike --source <feed-id>    # fetch one source and print parsed samples
.venv/bin/python -m newsnest.runner --no-ai              # one fetch-and-store tick, without AI
```

There is also a dev container with its own project name, port `3096` and the local `./config` and
`./data` folders:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

## Before you open a pull request

- Run `python -m pytest` and `ruff check .`, and add a test for what you change.
- If your change makes a sentence in [How NewsNest works](docs/how-it-works.md) wrong, update it in the
  same PR.

## Your first PR: a local news pack for your country

Add a `LocalPack` to `LOCAL_PACKS` in [`newsnest/packs.py`](newsnest/packs.py) with 8 to 10 national
outlets across the spectrum, each checked with `newsnest.discovery.check_feed` (https, working, posting in
the last two weeks).

## Project layout

```
newsnest/
  web.py            FastAPI app: pages, settings, login, JSON endpoints
  runner.py         the 60-second loop: fetch due sources, then the AI steps
  fetchers/         RSS/Atom and GitHub-releases fetchers
  pipeline.py       store items, de-duplicate by URL, record source health
  grouping.py       group the same story across outlets
  scoring.py        AI scoring and the final score
  summaries.py      key points, fetched from the article text
  feed.py           ranking and the home feed
  profile*.py       interests, drafting, and the weekly suggestions
  discovery.py      OPML import and sources on probation
  db.py             SQLite schema and migrations
  templates/        Jinja templates      static/  CSS, JS, fonts, icons
config.example/     starter config, copied to config/ on first start
docs/               user docs; design notes in docs/design/
tests/              pytest suite
```

[How NewsNest works](docs/how-it-works.md#for-contributors) maps each step to its module, the words the
code uses are defined in [GLOSSARY.md](GLOSSARY.md), and the reasoning behind bigger features is in
[docs/design/](docs/design/).
