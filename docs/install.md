# Installing NewsNest

The [Quick start](../README.md#quick-start) runs NewsNest with Docker volumes. This page covers the other
ways to run it, the environment variables, updates, backups and installing it on your phone.

You need [Docker](https://docs.docker.com/get-docker/) with Compose, and, for the AI features, an
[Anthropic API key](https://console.anthropic.com/) (or another provider, see
[Choosing an AI provider](configuration.md#choosing-an-ai-provider)).

The published image is multi-arch (`linux/amd64` + `linux/arm64`, so a Raspberry Pi or an ARM NAS works
too):

- GHCR: `ghcr.io/lucifix/newsnest`
- Docker Hub: `docker.io/lucifix/newsnest`

## With host folders

Use [`docker-compose.yml`](../docker-compose.yml) from this repo to keep the config and the database in
folders you can open and edit, instead of Docker volumes:

```bash
mkdir newsnest && cd newsnest
curl -fsSLO https://raw.githubusercontent.com/Lucifix/newsnest/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/Lucifix/newsnest/main/.env.example -o .env
mkdir -p config data     # so Docker doesn't create them as root
# edit .env: set NEWSNEST_PASSWORD, and ANTHROPIC_API_KEY for AI
docker compose up -d
```

The folders must be writable by the container user (`PUID`/`PGID`, `1000` by default). If Docker
created them as root, NewsNest stops with "… is not writable by uid …": fix it with
`sudo chown -R 1000:1000 config data`.

**Update:** `docker compose pull && docker compose up -d`. Set `NEWSNEST_IMAGE_TAG` in `.env` to pin a
release such as `0.8.0`; the default is `latest`.

## Build from source

```bash
git clone https://github.com/Lucifix/newsnest.git && cd newsnest
cp .env.example .env     # set NEWSNEST_PASSWORD, and ANTHROPIC_API_KEY for AI
mkdir -p config data     # so Docker doesn't create them as root
docker compose up -d --build
```

**Update:** `git pull && docker compose up -d --build`

## Portainer

The Quick start snippet works as a stack in the web editor (**Stacks → Add stack → Web editor**). To
deploy from the repository instead:

1. Create the host folders, owned by the container user:
   ```bash
   sudo mkdir -p /opt/newsnest/config /opt/newsnest/data
   sudo chown -R 1000:1000 /opt/newsnest
   ```
2. **Stacks → Add stack → Repository**: use this repository's URL, reference `refs/heads/main`, compose
   path `docker-compose.yml`.
3. Add the environment variables `NEWSNEST_PASSWORD` and `ANTHROPIC_API_KEY`.
4. Deploy. Portainer pulls the published image from `ghcr.io`; nothing is built on your server.
5. Check `http://<server>:3095/health`, then open `http://<server>:3095/`.

To update, choose **Pull and redeploy** on the stack, with *Re-pull image* turned on. The config and
database live in `/opt/newsnest` and survive redeploys.

## Settings in `.env`

The password, API keys and `TZ` are explained under the
[Quick start snippet](../README.md#quick-start). With the repo's
[`docker-compose.yml`](../docker-compose.yml), you set them in `.env` (see [`.env.example`](../.env.example))
or in the Portainer stack's environment, along with these, which fill in that file's `ports:`, `volumes:`,
`user:` and `image:` lines:

| Variable | Default | Purpose |
| --- | --- | --- |
| `NEWSNEST_PORT` | `3095` | Port the web UI is published on. |
| `NEWSNEST_CONFIG_PATH` | `/opt/newsnest/config` | Host folder for `config.yaml` and the profile files. |
| `NEWSNEST_DATA_PATH` | `/opt/newsnest/data` | Host folder for the database. |
| `PUID` / `PGID` | `1000` | User and group the container runs as. |
| `NEWSNEST_IMAGE_TAG` | `latest` | Image version to pull. |

`.env.example` sets the two paths to `./config` and `./data`.

These only work through `.env` or the stack's environment. Don't put them under `environment:` in a compose
file: there, `NEWSNEST_PORT` changes the port NewsNest listens on inside the container, and it can no
longer be reached on `8080`.

`NEWSNEST_LOG_LEVEL` (default `INFO`) sets the log level, and goes under `environment:` like the password.

## Outside your home network

The login is a single shared password over plain HTTP, which is fine on your home network. If you expose
NewsNest to the internet, put it behind an HTTPS reverse proxy.

## Install it as an app on your phone

NewsNest is a progressive web app: it gets a home-screen icon, opens full screen, and keeps your most
recent pages available offline. Browsers only offer to install over **HTTPS**, so open it through an
https address, for example one served by a reverse proxy, rather than `http://<server>:3095`.

- **Android (Chrome):** menu → *Install app*, or **Settings → Install** inside NewsNest.
- **iPhone (Safari):** Share → *Add to Home Screen*.

## Backup and restore

**Backup:** open **Settings → App & backup → Download database backup**. It gives you a
single consistent `.db` file with all stories, ratings and history, and it is safe to take while the app
is running. Copy the `config/` folder too: it holds your feeds and interests, which the download does
not include.

**Restore:** stop the container, put the backup in your data folder as `newsnest.db`, and start it again.
With Docker volumes (the Quick start snippet), copy it in while the container is stopped:
`docker cp backup.db newsnest:/data/newsnest.db`.

**Automatic backups before upgrades:** when a new release changes the database layout, NewsNest first
copies the database to `backups/` in your data folder (for example
`backups/newsnest-20261107-093000-v16.db`, where `v16` is the layout it had). It keeps the three newest
copies.

## Going back to an older release

Newer releases can add settings to `config.yaml` (such as `ai:` or `sections:`) and change the database
layout, which older releases don't understand. To go back:

1. Stop the container.
2. Restore `config.yaml.bak` from your config folder (the copy kept on every edit made in Settings), or
   remove the newer keys by hand.
3. If the database was upgraded, put the matching copy from `backups/` back as `newsnest.db`.
4. Start the older release.

If you start an older release on a database a newer one has already upgraded, NewsNest refuses to start
and says so in the log, rather than running on a layout it doesn't know.
