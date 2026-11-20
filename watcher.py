"""Watch OpenAI product releases and SEC press releases / speeches, and push new
posts to your phone through ntfy (https://ntfy.sh).

Runs from GitHub Actions every ~5 minutes. Uses only the Python standard library.

Settings come from repository secrets (see README):
  NTFY_TOPIC       your private ntfy topic name (required)
  SEC_USER_AGENT   "Your Name your@email.com"; the SEC asks automated tools to identify themselves
"""

import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

SEEN_FILE = Path(__file__).with_name("seen.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
SEC_UA = os.environ.get("SEC_USER_AGENT", "").strip() or "post-watcher contact@example.com"
BROWSER_UA = "Mozilla/5.0 (compatible; post-watcher/1.0)"

SOURCES = [
    # (source key, label, kind, url)
    ("openai", "OpenAI product release", "openai_rss", "https://openai.com/news/rss.xml"),
    ("sec-press", "SEC press release", "sec_page", "https://www.sec.gov/newsroom/press-releases"),
    ("sec-press", "SEC press release", "rss", "https://www.sec.gov/news/pressreleases.rss"),
    ("sec-speech", "SEC speech/statement", "sec_page", "https://www.sec.gov/newsroom/speeches-statements"),
    ("sec-speech", "SEC speech/statement", "rss", "https://www.sec.gov/news/speeches-statements.rss"),
]

SEC_ITEM_PATH = {
    "https://www.sec.gov/newsroom/press-releases": "/newsroom/press-releases/",
    "https://www.sec.gov/newsroom/speeches-statements": "/newsroom/speeches-statements/",
}


def fetch(url):
    ua = SEC_UA if "sec.gov" in url else BROWSER_UA
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_rss(xml_text, product_only=False):
    items = []
    root = ET.fromstring(xml_text)
    for item in root.iter("item"):
        title = clean(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        if product_only:
            cats = [clean(c.text) for c in item.findall("category")]
            if "Product" not in cats:
                continue
        items.append({"title": title, "url": link})
    return items


def parse_sec_page(page_html, page_url):
    """Pull item links out of an SEC newsroom listing page."""
    prefix = SEC_ITEM_PATH[page_url]
    items = []
    for href, text in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page_html, re.S):
        href = html.unescape(href)
        if href.startswith("/"):
            href = "https://www.sec.gov" + href
        if not href.startswith("https://www.sec.gov" + prefix):
            continue
        slug = href[len("https://www.sec.gov" + prefix):]
        # skip the section index, pagination, sorting and the archive page
        if not slug or "?" in slug or "#" in slug or "/" in slug.strip("/") or slug.endswith("-archive"):
            continue
        title = clean(text)
        if title:
            items.append({"title": title, "url": href})
    return items


def collect():
    found, failures = {}, []
    for key, label, kind, url in SOURCES:
        try:
            body = fetch(url)
            if kind == "openai_rss":
                items = parse_rss(body, product_only=True)
            elif kind == "rss":
                items = parse_rss(body)
            else:
                items = parse_sec_page(body, url)
            if not items:
                failures.append(f"{url}: no items found")
            for it in items:
                found.setdefault(it["url"], {**it, "source": key, "label": label})
        except Exception as exc:  # keep checking the other sources
            failures.append(f"{url}: {exc}")
    return found, failures


def notify(title, message, click=None, tags="green_circle"):
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message, "tags": [tags]}
    if click:
        payload["click"] = click
    req = urllib.request.Request(
        "https://ntfy.sh/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=30).read()


def main():
    if not NTFY_TOPIC:
        sys.exit("NTFY_TOPIC secret is not set")

    first_run = not SEEN_FILE.exists()
    seen = {} if first_run else json.loads(SEEN_FILE.read_text())

    found, failures = collect()
    for f in failures:
        print("WARN", f)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = [it for url, it in found.items() if url not in seen]

    if first_run:
        # Record everything currently listed so you are not flooded with old posts.
        for it in new:
            seen[it["url"]] = {"title": it["title"], "source": it["source"], "first_seen": now}
        notify("Post watcher is live",
               f"Watching OpenAI product releases and SEC press releases + speeches. "
               f"{len(new)} existing posts recorded; you'll be pinged only for new ones.",
               tags="white_check_mark")
    else:
        for it in new:
            notify(f"NEW {it['label']}", it["title"], click=it["url"])
            seen[it["url"]] = {"title": it["title"], "source": it["source"], "first_seen": now}
            print("NEW", it["url"])

    SEEN_FILE.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n")
    print(f"checked {len(found)} items, {len(new)} new, {len(failures)} source problems")


if __name__ == "__main__":
    main()
