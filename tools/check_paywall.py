"""Check which articles a publisher marks as for subscribers, the way NewsNest's lock label sees them (dev tool).

    python3 tools/check_paywall.py https://www.example.com/some-article ...
    python3 tools/check_paywall.py --feed https://www.theverge.com/rss/index.xml --limit 10

Loads each page once with NewsNest's own user agent and prints "paid", "free" or the error, plus what the page's
structured data says (isAccessibleForFree). Waits a second between pages, as the summaries do.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import feedparser  # noqa: E402

from newsnest.config import Config  # noqa: E402
from newsnest.fetchers.base import FetchContext, FetchFailed, http_get, make_client  # noqa: E402
from newsnest.summaries import publisher_marks_paid  # noqa: E402

FLAG = re.compile(r'isAccessibleForFree"?\s*(?::|content=)\s*"?(\w+)', re.I)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("urls", nargs="*", help="article URLs")
    ap.add_argument("--feed", help="an RSS/Atom feed: check its newest articles")
    ap.add_argument("--limit", type=int, default=8, help="articles to check from --feed (default 8)")
    args = ap.parse_args()
    cfg = Config()
    with make_client(cfg) as client:
        ctx = FetchContext(cfg, client)
        urls = list(args.urls)
        if args.feed:
            feed = feedparser.parse(http_get(ctx, args.feed).content)
            urls += [e.link for e in feed.entries[:args.limit] if e.get("link")]
        if not urls:
            ap.error("give article URLs or --feed")
        for i, url in enumerate(urls):
            if i:
                time.sleep(1)
            try:
                html = http_get(ctx, url).text
            except FetchFailed as e:
                print(f"error  {url}\n       {e}")
                continue
            flags = sorted({m.lower() for m in FLAG.findall(html)})
            print(f"{'paid ' if publisher_marks_paid(html) else 'free '}  {url}\n"
                  f"       isAccessibleForFree: {', '.join(flags) or 'not in the page'}")


if __name__ == "__main__":
    main()
