# Configuring NewsNest

Most settings can be changed in the app, under **Settings** and **Profile**. They are stored as plain
files in the `config/` folder, which is re-read on every run, so there is nothing to restart after editing
one by hand.

| File | Purpose |
| --- | --- |
| `config.yaml` | Feeds, GitHub repos, watches, schedules, thresholds, models and the spending cap. Every option is explained in [`config.example/config.yaml`](../config.example/config.yaml). |
| `interests.md` | What you care about and what to skip. Written on the Profile page, or by hand. |
| `seed_examples.md` | About 20 example headlines marked `+` (want) or `-` (don't want). |

Edits made in the Settings pages are written back to `config.yaml`, comments included, and the previous
version is kept as `config.yaml.bak`.

How much of each interest you want (the level next to it on Profile → *Your interests*) isn't in any of these
files: it's kept in the database, so the AI never reads it and an accepted suggestion can't drop it. A level
follows its line when you reword it on the Profile page or accept a suggestion that rewords it, and is removed
when its line leaves `interests.md` (also when you change the file as text or by hand).

On first start NewsNest copies the starter config from [`config.example/`](../config.example/), with
international news and tech feeds, no local ones, no region and the time zone `UTC`. Set where you live
in **Settings → General** (country, city, time zone), and NewsNest offers your country's local pack.

Environment variables (password, API keys, ports, folders) are listed in
[Installing NewsNest](install.md#settings-in-env).

## Sections

Every feed belongs to one section, and the sections are the tabs along the bottom of the feed. A new
install starts with Local, World and Tech. In **Settings → Feeds → Sections** you can rename them, add
your own (Sports, Science, Cinema…), reorder them, and remove one (its feeds and stories move to a section
you pick). Local and World are built in: they can be renamed but not removed, and share the News tab. A
section is a tab once it has a feed; at most four can be tabs, and any further section shows as a chip
under **For you**.

The list is the `sections:` key in `config.yaml`. A `config.yaml` without one keeps the original four
sections (Tech, Local, World, Lifestyle).

## Choosing an AI provider

Claude through the Anthropic API is the default and needs nothing beyond `ANTHROPIC_API_KEY`. To use
something else, add an `ai:` section to `config.yaml` and set `scoring.model` and `scoring.profile_model`
to models that provider serves. `provider: openai` speaks the OpenAI API, which OpenAI, OpenRouter, Groq,
DeepSeek, Gemini, Ollama, LM Studio and vLLM all offer.

```yaml
# Ollama in the same stack (or http://host.docker.internal:11434/v1 for Ollama on this machine)
ai:
  provider: openai
  base_url: http://ollama:11434/v1
  timeout_seconds: 600
  prices: {"*": [0, 0]}      # local: free, so the budget never stops it
  profile:                   # optional: keep Claude for the rare profile jobs
    provider: anthropic
scoring:
  model: qwen3:14b
  profile_model: claude-opus-5-5
```

| Provider | `base_url` | Key |
| --- | --- | --- |
| OpenAI | *(empty)* | `OPENAI_API_KEY` |
| OpenRouter | `https://openrouter.ai/api/v1` | `api_key_env: OPENROUTER_API_KEY` (add it to the compose environment) |
| Ollama / LM Studio | `http://<host>:11434/v1` / `http://<host>:1234/v1` | none |

With a `base_url` the key is optional, since local servers need none, unless `api_key_env` names one: then a
missing variable shows as "… is not set" on the status page instead of a 401 from the API.

Things to know before switching:

- **Quality.** Scoring, grouping by event and the profile draft were tuned on Claude. Small local models
  (8B and below) rank noticeably worse and sometimes skip stories; 14B and up do reasonably. Keep a strong
  model for `profile_model`, through `ai.profile` if needed.
- **Prices.** Claude models are priced built in. Any other model counts at Claude Opus rates until you
  set its price under `ai.prices` (USD per million tokens, `[input, output]`), so the daily budget runs out
  early. The status page warns when a price is missing.
- **Ollama on the host** listens on `127.0.0.1` only: start it with `OLLAMA_HOST=0.0.0.0` so the container
  can reach it. Raise its context window (`OLLAMA_CONTEXT_LENGTH=16384` or more), or the scoring prompt is
  cut off silently. On a CPU, raise `timeout_seconds`.
- **Structured output.** NewsNest asks for a strict JSON schema. A server that rejects it needs
  `json_mode: json`. Reasoning models get `scoring.effort` only with `send_effort: true`.
