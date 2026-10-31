# Open items

## Before relying on v1
- [ ] First prod deploy: check /status → "AI usage today" shows successful calls, nonzero cache reads, sane cost.
- [ ] Visual check of every page at phone width, dark and light (not yet done in a browser).
- [ ] Backups: /opt/newsnest/data is outside ${DOCKER_PATH}; add it to the backup stack or move it.
      (Settings → Install, backup & log out has a manual database download button.)
- [ ] Docker image build verified only via Portainer (dev user can't reach the Docker daemon).

## Later (step 5)
- [ ] Home Assistant notifier behind newsnest.notify (top news, quiet hours, caps).
- [ ] Remote access (VPN or NPM + auth). Never expose without auth.
- [ ] Optional: GitHub Actions build → GHCR, stack only pulls.

## More sources (later; none verified yet, check for an https RSS feed first)
Learned outlet weights (card menu → "Less from …") keep a noisy new source in check.
- Slovenian news: Ljubljana Info (ljubljanainfo.com), Necenzurirano, Forbes Slovenija, Bloomberg Adria
  (timed out once), RTV/24ur/Siol per-section feeds (e.g. RTV Ljubljana, Zdravje) for more depth.
- No RSS found earlier: Finance, Primorske novice, STA (paid), Svet24, MOL news page (link returns HTML).
- Health/science: RTV Znanost in tehnologija, 24ur Zdravje, Quanta Magazine, Nature News.
- Tech / self-hosting: The Register, Wired, How-To Geek, XDA, selfh.st weekly, LWN, Phoronix,
  more GitHub release feeds for apps on a home server (Immich, Portainer, Zigbee2MQTT, Uptime Kuma…).

## Feed ideas not built yet
- [ ] PWA (home-screen app, pull to refresh, offline shell): needs HTTPS via NPM or VPN.
- [x] Swipe section (Tinder-style deck at /swipe: right 👍, left not interested, up later, tap opens).
- [ ] Swipe gestures on the feed cards themselves (not built; the deck is a separate section).
- [ ] Search: Slovene word forms (Ljubljana should find Ljubljani). Today whole words match, plus the
      last word as a prefix while typing. Options: a Slovene stemmer, or the FTS5 trigram tokenizer.

## Known limitations
- Story grouping is headline-based; occasional false merges (both headlines stay visible on the card).
- Most feeds hold ~1 day, so the first-run backfill is "whatever feeds hold" (max_item_age_days: 3).
