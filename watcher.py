"""Watch a set of news pages and feeds, and push each new post to ntfy (https://ntfy.sh).

Runs from GitHub Actions (started every ~5 minutes). Uses only the Python standard library.

Settings come from repository secrets (see README):
  NTFY_TOPIC       your private ntfy topic name (required)
  SEC_USER_AGENT   "Your Name your@email.com"; the SEC asks automated tools to identify themselves
  CONGRESS_API_KEY optional free key from api.congress.gov; makes Congressional Record alerts
                   as fast as congress.gov (without it, GovInfo is used, which runs ~2 days behind)

To add a source, add a line to SOURCES. The first time a source is read successfully,
everything already on it is recorded quietly, so you only get pinged for later posts.
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
from urllib.parse import urljoin, urlsplit

SEEN_FILE = Path(__file__).with_name("seen.json")
META_KEY = "__meta__"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
CONGRESS_API_KEY = os.environ.get("CONGRESS_API_KEY", "").strip()
SEC_UA = os.environ.get("SEC_USER_AGENT", "").strip() or "post-watcher contact@example.com"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 post-watcher/1.1")

# Each source: key, alert label, kind, url, options.
#   kind "rss"  : RSS/Atom feed. option "category" keeps only items in that category.
#   kind "sitemap": the site's sitemap.xml; option "path" as for "page". Titles are looked up
#                 from the post itself when it's new.
#   kind "page" : listing page. option "path" is the URL path prefix of individual posts;
#                 only links of the form <path><one-segment-slug> count.
# Several entries may share a key (e.g. a page and its feed); results are merged.
SOURCES = [
    ("openai", "OpenAI product release", "rss", "https://openai.com/news/rss.xml", {"category": "Product"}),

    ("sec-press", "SEC press release", "page", "https://www.sec.gov/newsroom/press-releases",
     {"path": "/newsroom/press-releases/"}),
    ("sec-press", "SEC press release", "rss", "https://www.sec.gov/news/pressreleases.rss", {}),
    ("sec-speech", "SEC speech/statement", "page", "https://www.sec.gov/newsroom/speeches-statements",
     {"path": "/newsroom/speeches-statements/"}),
    ("sec-speech", "SEC speech/statement", "rss", "https://www.sec.gov/news/speeches-statements.rss", {}),

    ("usmint", "US Mint press release", "page", "https://www.usmint.gov/news/press-releases",
     {"path": "/news/press-releases/"}),
    ("nvidia", "NVIDIA blog post", "rss", "https://blogs.nvidia.com/feed/", {}),
    # Anthropic's research table is built by JavaScript, so read the site's sitemap instead
    ("anthropic", "Anthropic research", "sitemap", "https://www.anthropic.com/sitemap.xml",
     {"path": "/research/"}),
    # Anthropic News: the news page (freshest) plus the sitemap (complete). Big launches
    # sometimes live at top-level addresses (e.g. /claude-fable-and-mythos-5-2), so the
    # news page is also read for top-level links.
    ("anthropic-news", "Anthropic news", "page", "https://www.anthropic.com/news", {"path": "/news/"}),
    ("anthropic-news", "Anthropic news", "page", "https://www.anthropic.com/news", {"path": "/"}),
    ("anthropic-news", "Anthropic news", "sitemap", "https://www.anthropic.com/sitemap.xml",
     {"path": "/news/"}),
    ("neuralink", "Neuralink update", "page", "https://neuralink.com/updates/", {"path": "/updates/"}),
    ("xai", "xAI news", "page", "https://x.ai/news", {"path": "/news/"}),
    # Congressional Record. congress.gov's pages don't allow automated reading, but its official
    # API does (needs CONGRESS_API_KEY). GovInfo (the official publisher) is the no-key fallback.
    # All three are matched up by issue date, so one issue pings once.
    ("crec", "Congressional Record issue", "congress_api",
     "https://api.congress.gov/v3/daily-congressional-record", {}),
    ("crec", "Congressional Record issue", "crec_govinfo", "https://www.govinfo.gov/sitemap/CREC_{year}_sitemap.xml", {}),
    ("crec", "Congressional Record issue", "crec_govinfo", "https://www.govinfo.gov/rss/crec.xml", {}),
]


def fetch(url):
    ua = SEC_UA if "sec.gov" in url else BROWSER_UA
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_feed(xml_text, category=None):
    """RSS 2.0 or Atom."""
    items = []
    root = ET.fromstring(xml_text)
    atom = "{http://www.w3.org/2005/Atom}"
    for item in root.iter("item"):
        title = clean(item.findtext("title"))
        link = (item.findtext("link") or "").strip()
        if category and category not in [clean(c.text) for c in item.findall("category")]:
            continue
        if title and link:
            items.append({"title": title, "url": link})
    for entry in root.iter(atom + "entry"):
        title = clean(entry.findtext(atom + "title"))
        link_el = entry.find(atom + "link")
        link = (link_el.get("href") if link_el is not None else "") or ""
        if title and link:
            items.append({"title": title, "url": link.strip()})
    return items


def post_url(url, host, path):
    """Return a cleaned post URL if url is <host><path><one-segment-slug>, else None."""
    parts = urlsplit(url)
    if parts.netloc.lower().removeprefix("www.") != host or parts.query:
        return None
    if not parts.path.startswith(path):
        return None
    slug = parts.path[len(path):].strip("/")
    # one-segment slugs only: skips the index page, pagination, categories, team pages
    if (not slug or "/" in slug or slug.isdigit() or slug.endswith("archive") or slug.endswith("archives")
            or slug in {"page", "category", "tag", "feed", "team"}):
        return None
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def slug_title(url):
    return url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").capitalize()


def parse_sitemap(xml_text, page_url, path):
    host = urlsplit(page_url).netloc.lower().removeprefix("www.")
    urls = {post_url(html.unescape(u.strip()), host, path) for u in re.findall(r"<loc>(.*?)</loc>", xml_text, re.S)}
    return [{"title": slug_title(u), "url": u, "needs_title": True} for u in sorted(u for u in urls if u)]


def page_title(url):
    """Best-effort real title of a post, for alerts."""
    try:
        body = fetch(url)
        m = (re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', body, re.I)
             or re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I))
        if m:
            return clean(m.group(1))
    except Exception:
        pass
    return None


def parse_page(page_html, page_url, path):
    """Pull post links out of a listing page: links to <same host><path><slug>."""
    host = urlsplit(page_url).netloc.lower().removeprefix("www.")
    best = {}
    for href, text in re.findall(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page_html, re.S | re.I):
        url = post_url(urljoin(page_url, html.unescape(href)).split("#")[0], host, path)
        if not url:
            continue
        title = clean(text)
        # keep the longest text seen for the URL (cards often have an image link + a title link)
        if url not in best or len(title) > len(best[url]):
            best[url] = title
    return [{"title": t or slug_title(u), "url": u} for u, t in best.items()]


def collect():
    found = {}       # url -> item
    ok_keys = set()  # sources that returned at least one item
    problems = {}    # key -> list of messages
    labels = {}
    for key, label, kind, url, opts in SOURCES:
        labels[key] = label
        if kind == "congress_api":
            if not CONGRESS_API_KEY:
                continue  # optional source
            url = f"{url}?format=json&limit=20&api_key={CONGRESS_API_KEY}"
        url = url.replace("{year}", str(datetime.now(timezone.utc).year))
        try:
            body = fetch(url)
            if kind == "congress_api":
                items = parse_congress_api(body)
            elif kind == "crec_govinfo":
                items = parse_crec_govinfo(body)
            elif kind == "rss":
                items = parse_feed(body, opts.get("category"))
            elif kind == "sitemap":
                items = parse_sitemap(body, url, opts["path"])
            else:
                items = parse_page(body, url, opts["path"])
            if items:
                ok_keys.add(key)
            else:
                problems.setdefault(key, []).append(f"{url}: no posts found")
            for it in items:
                # "id" lets different sites report the same item once (e.g. one Record issue)
                found.setdefault(it.get("id") or it["url"], {**it, "source": key, "label": label})
        except Exception as exc:
            msg = f"{url}: {exc}"
            if CONGRESS_API_KEY:
                msg = msg.replace(CONGRESS_API_KEY, "***")  # never print the key in logs
            problems.setdefault(key, []).append(msg)
    # a source counts as failing only if every one of its URLs failed
    failing = {k: v for k, v in problems.items() if k not in ok_keys}
    return found, ok_keys, failing, labels


CREC_BROWSE = "https://www.congress.gov/congressional-record/browse-by-date"


def crec_item(date_str, issue=None, url=None):
    d = datetime.strptime(date_str[:10], "%Y-%m-%d")
    title = f"Congressional Record for {d:%b} {d.day}, {d.year}" + (f" (No. {issue})" if issue else "")
    return {"id": f"CREC-{d:%Y-%m-%d}", "title": title, "url": url or CREC_BROWSE}


def parse_congress_api(body):
    items = []
    for rec in json.loads(body).get("dailyCongressionalRecord", []):
        date = rec.get("issueDate") or ""
        if re.match(r"\d{4}-\d{2}-\d{2}", date):
            items.append(crec_item(date, rec.get("issueNumber")))
    return items


def parse_crec_govinfo(body):
    ids = set(re.findall(r"https://www\.govinfo\.gov/app/details/CREC-(\d{4}-\d{2}-\d{2})", body))
    return [crec_item(d, url=f"https://www.govinfo.gov/app/details/CREC-{d}") for d in sorted(ids)]


def notify(title, message, click=None, tags="green_circle"):
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message, "tags": [tags]}
    if click:
        payload["click"] = click
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()


def main():
    if not NTFY_TOPIC:
        sys.exit("NTFY_TOPIC secret is not set")

    seen = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}
    meta = seen.pop(META_KEY, {})
    # older versions keyed Record issues by their GovInfo link; switch to the issue id
    for k in [k for k in seen if "govinfo.gov/app/details/CREC-" in k]:
        seen[k.rsplit("/", 1)[-1]] = seen.pop(k)
    seeded = set(meta.get("seeded", [])) | {v.get("source") for v in seen.values() if isinstance(v, dict)}
    # remove leftovers from the old YouTube search
    for k in [k for k in seen if k.startswith("yt:")]:
        del seen[k]
    for k in ("yt_last", "yt_started"):
        meta.pop(k, None)
    seeded.discard("youtube")
    warned = set(meta.get("warned", []))

    found, ok_keys, failing, labels = collect()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for key, msgs in failing.items():
        for m in msgs:
            print("WARN", m)

    # 1. sources read for the first time: record quietly, one confirmation ping
    new_sources = sorted(k for k in ok_keys if k not in seeded)
    for key in new_sources:
        items = [(k, it) for k, it in found.items() if it["source"] == key and k not in seen]
        for k, it in items:
            seen[k] = {"title": it["title"], "source": key, "first_seen": now}
        seeded.add(key)
        notify(f"Now watching: {labels[key]}",
               f"{len(items)} existing posts recorded; you'll be pinged only for new ones.",
               tags="white_check_mark")

    # 2. genuinely new posts on sources already being watched
    new = [(k, it) for k, it in found.items() if k not in seen and it["source"] in seeded]
    for k, it in new:
        if it.get("needs_title"):
            it["title"] = page_title(it["url"]) or it["title"]
        notify(f"NEW {it['label']}", it["title"], click=it["url"])
        seen[k] = {"title": it["title"], "source": it["source"], "first_seen": now}
        print("NEW", k)

    # 3. one-time heads-up when a source can't be read (cleared once it works again)
    for key in failing:
        if key not in warned:
            notify(f"Can't read: {labels[key]}",
                   "The watcher couldn't read this site. You won't get alerts for it until that's fixed.",
                   tags="warning")
            warned.add(key)
    warned -= ok_keys

    seen[META_KEY] = {**meta, "seeded": sorted(seeded), "warned": sorted(warned)}
    SEEN_FILE.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n")
    print(f"checked {len(found)} items, {len(new)} new, {len(new_sources)} sources added, "
          f"{len(failing)} sources failing")


if __name__ == "__main__":
    main()
