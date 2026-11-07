<div align="center">

<img src="https://raw.githubusercontent.com/Lucifix/newsnest/main/newsnest/static/icons/icon-192.png" width="96" height="96" alt="NewsNest logo">

# NewsNest

**A self-hosted news feed that learns what you care about.**

NewsNest gathers your RSS feeds into one calm stream, folds the same story from different outlets
into a single card, and uses Claude (or a model of your choice, even a local one) to rank and summarise
what matters to you.

[![Latest release](https://img.shields.io/github/v/release/Lucifix/newsnest)](https://github.com/Lucifix/newsnest/releases/latest)
![License: MIT](https://img.shields.io/github/license/Lucifix/newsnest)
[![Docker pulls](https://img.shields.io/docker/pulls/lucifix/newsnest?logo=docker&logoColor=white)](https://hub.docker.com/r/lucifix/newsnest)
[![Build status](https://img.shields.io/github/actions/workflow/status/Lucifix/newsnest/docker-publish.yml?branch=main&label=build)](https://github.com/Lucifix/newsnest/actions/workflows/docker-publish.yml)

[Website](https://lucifix.github.io/newsnest/) · [Features](#features) · [Quick start](#quick-start) · [How it works](docs/how-it-works.md) · [Install](docs/install.md) · [Configuration](docs/configuration.md) · [Contributing](CONTRIBUTING.md)

<br>

<table>
  <tr>
    <td align="center" width="33%"><img src="https://raw.githubusercontent.com/Lucifix/newsnest/main/docs/images/feed.png" alt="The For you feed with a top story, key points and a score"></td>
    <td align="center" width="33%"><img src="https://raw.githubusercontent.com/Lucifix/newsnest/main/docs/images/swipe.png" alt="The swipe deck for sorting stories with one thumb"></td>
    <td align="center" width="33%"><img src="https://raw.githubusercontent.com/Lucifix/newsnest/main/docs/images/feed-dark.png" alt="The same feed in dark mode"></td>
  </tr>
  <tr>
    <td align="center"><sub>Feed, ranked for you</sub></td>
    <td align="center"><sub>Swipe to sort</sub></td>
    <td align="center"><sub>Dark mode, automatically</sub></td>
  </tr>
  <tr>
    <td align="center" colspan="3"><img src="https://raw.githubusercontent.com/Lucifix/newsnest/main/docs/images/desktop.png" alt="The home page on a desktop: a front page with the top story and its score, Top stories below it, and the ranked feed beside it"></td>
  </tr>
  <tr>
    <td align="center" colspan="3"><sub>A front page on bigger screens</sub></td>
  </tr>
</table>

<sub>Outlets and headlines are made up. Photos from <a href="https://unsplash.com">Unsplash</a> by
<a href="https://unsplash.com/@jbl12761">James Lee</a>, <a href="https://unsplash.com/@amoltyagi2">Amol Tyagi</a>,
<a href="https://unsplash.com/@gabiontheroad">Gabriella Clare Marino</a> and <a href="https://unsplash.com/@afgprogrammer">Mohammad Rahmani</a>.</sub>

</div>

## Why NewsNest?

Checking a dozen news sites every day is slow, and most of what you see isn't for you. Algorithmic
feeds fix that by learning what keeps you scrolling, which is not the same as what you want to read.

NewsNest is the opposite deal. **You choose the sources, you write down what interests you, and you can
see why every story ranks where it does.** It runs on your own machine, keeps everything in one SQLite
file, and talks to the Anthropic API with your own key, or to any OpenAI-compatible API, including a
local model in Ollama.

## Features

- **Your own sources.** Any RSS or Atom feed, GitHub release feeds and OPML import. New feeds go on
  probation and only earn a place once you read them.
- **Ranked for you.** The AI scores each story on topic, importance and personal fit against an
  interests file that you write yourself or have drafted from a few ratings.
- **One story, not five.** The same event from several outlets becomes one card, and wide coverage
  lifts it up.
- **Key points on demand.** A one-line lede, a few bullets, why it matters to you, and whether the full
  article is worth your time.
- **Learns from how you read.** Likes, opens and what you linger on move topics up or down, never out.
  Weekly suggestions for your interests file wait for your OK.
- **No black box.** Every card shows why it ranks where it does, calibration mode shows every score, and
  *Missed this* takes a link the feed should have shown you.
- **Swipe to sort.** A one-thumb deck of the stories NewsNest is least sure about, so a few minutes of
  rating teaches it the most.
- **A hard spending cap.** AI calls stop for the day at your budget (default $0.50). Without an API key
  everything still works, newest first.

<details>
<summary>And more</summary>

- A front page on laptops and desktops: your top story large, then Top stories and a block per category
- An installable web app with light and dark themes, password-protected, with a one-click database backup
- Keyword **watches** that always surface a topic, plus mute and must-show keyword filters
- Full-text **search**, **saved** stories, a **read** history and a day-by-day **archive**
- A status page and a `/health` endpoint that report feeds that fail or go silent
- Per-source daily caps, so a firehose feed can't drown out everything else
- Summaries in the language of your choice

</details>

## Quick start

You need [Docker](https://docs.docker.com/get-docker/) with Compose, and, for the AI features, an
[Anthropic API key](https://console.anthropic.com/) (or another provider, see
[Choosing an AI provider](docs/configuration.md#choosing-an-ai-provider)).

Save this as `docker-compose.yml`, or paste it into Portainer (**Stacks → Add stack → Web editor**). Only
the password needs filling in:

```yaml
services:
  newsnest:
    image: ghcr.io/lucifix/newsnest:latest
    container_name: newsnest
    restart: unless-stopped
    ports:
      - 3095:8080
    environment:
      NEWSNEST_PASSWORD: change-me   # required: with no password the app refuses all access
      ANTHROPIC_API_KEY: ""          # optional: enables ranking and summaries
      TZ: UTC                        # time zone for the logs, e.g. Europe/Ljubljana
    volumes:
      - newsnest-config:/config
      - newsnest-data:/data

volumes:
  newsnest-config:
  newsnest-data:
```

| Setting | What it does |
| --- | --- |
| `3095:8080` | The port you open NewsNest on. Change only the left number, e.g. `8000:8080`. |
| `NEWSNEST_PASSWORD` | **Required.** Password for the web UI. With none set, access is refused. |
| `ANTHROPIC_API_KEY` | Turns on ranking, key points and the weekly suggestions with Claude. |
| `OPENAI_API_KEY` | For OpenAI and other providers, including local models: see [Choosing an AI provider](docs/configuration.md#choosing-an-ai-provider). |
| `TZ` | Time zone for the logs. NewsNest's own time zone is set in **Settings → General**. |
| `/config` | Your feeds, interests and settings. |
| `/data` | The database. |

Then run `docker compose up -d`. The image runs on amd64 and arm64, so a Raspberry Pi or an ARM NAS works
too. **Update:** `docker compose pull && docker compose up -d`.

For host folders instead of Docker volumes, a Portainer stack from the repository, building from source
and backups, see [Installing NewsNest](docs/install.md).

### First run

Open <http://localhost:3095>, log in with your password, and tap **Set up my profile**:

1. **Say where you live.** Your country and city tell the AI whose news is "big", and your time zone is
   picked up from the browser.
2. **Add local news.** NewsNest offers the local pack for your country (Australia, Austria, Canada,
   Croatia, France, Germany, Italy, Slovenia, the United Kingdom and the United States so far), or add your
   own feeds under Local in **Settings → Feeds**.
3. **Rate about 30 headlines**, and the AI drafts your interests from them.

The first fetch only gets what your feeds still hold, usually about a day of news, so the feed fills up
over the first day.

Everything else is in **Settings**, or in the files in `config/`: see
[Configuring NewsNest](docs/configuration.md).

> [!NOTE]
> The login is a single shared password over plain HTTP, which is fine on your home network. If you
> expose NewsNest to the internet, put it behind an HTTPS reverse proxy.

## How it works

NewsNest collects headlines from your feeds, groups the same story from different outlets into one card,
and has an AI score each one against your interests. The best rise to the top of your feed, which keeps
learning from how you read it. [How NewsNest works](docs/how-it-works.md) follows one real headline all
the way to your feed.

**What leaves your server:** headlines and snippets, and for key points the article text, go to your AI
provider with your own key. With a local model in Ollama, nothing leaves your network. Your feed list,
ratings and database stay on your machine, and NewsNest itself sends no telemetry.

## Documentation

- [How NewsNest works](docs/how-it-works.md): one headline's trip from an outlet to your feed
- [Installing NewsNest](docs/install.md): other ways to run it, environment variables, updates, backups,
  installing on your phone
- [Configuring NewsNest](docs/configuration.md): the config files, sections, and other AI providers
  including local models
- [Contributing](CONTRIBUTING.md): development setup, project layout, and an easy first PR

## AI-assisted development

NewsNest started as a personal project, built with the help of AI tools. Eventually, I decided to
open-source it so others could use it too.

## Contributing

Bug reports, ideas and pull requests are welcome. Found a bug? Settings → *About & report a bug* in the
app opens a new issue with your version filled in. Development setup, tests and an easy first PR (a local
news pack for your country) are in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE), except the bundled fonts in [`newsnest/static/fonts/`](newsnest/static/fonts/):
[JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) and
[Space Grotesk](https://github.com/floriankarsten/space-grotesk) are licensed under the
[SIL Open Font License 1.1](https://openfontlicense.org); their license files sit next to the fonts.
