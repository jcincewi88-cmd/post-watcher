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
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

SEEN_FILE = Path(__file__).with_name("seen.json")
META_KEY = "__meta__"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
CONGRESS_API_KEY = os.environ.get("CONGRESS_API_KEY", "").strip()
GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()  # GitHub's automatic per-run key (higher rate limits)
SEC_UA = os.environ.get("SEC_USER_AGENT", "").strip() or "post-watcher contact@example.com"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 post-watcher/1.1")

# --- YouTube transcript keyword alerts ---------------------------------------------------
# New uploads from these channels are sent to an Apify transcript tool; if the spoken words
# contain one of the keywords in keywords.txt, you get a ping that opens the video at that moment.
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "").strip()
APIFY_ACTOR = "vKlQCAJRI72MdyK1u"  # "Youtube Transcript Scraper" by supreme_coder
KEYWORDS_FILE = Path(__file__).with_name("keywords.txt")
YT_KEY = "yt-transcripts"
# minutes to wait before each new try while a video has no captions yet (then give up)
YT_RETRY_MINUTES = [10, 20, 40, 80, 160, 320]
YT_BATCH = 25  # most videos sent to Apify in one check
YT_ICON = "https://www.gstatic.com/youtube/img/branding/favicon/favicon_144x144.png"
YT_CHANNELS = [
    ("Fox Business", "UCCXoCcu9Rp7NPbTzIvogpZg"),
    ("Yahoo Finance", "UCEAZeUIeJs0IjQiqTCdVSIg"),
    ("Coin Bureau", "UCqK_GSMbpiV8spgD3ZGloSw"),
    ("CoinDesk", "UC7TghOL755nBk7HelHoi9LQ"),
    ("Bankless", "UCAl9Ld79qaZxp9JzEOwd3aA"),
    ("Altcoin Daily", "UCbLhGKVY-bJPcawebgtNfbw"),
    ("Crypto Banter", "UCN9Nj4tjXbVTLYWN0EKly_Q"),
    ("Joe Rogan (PowerfulJRE)", "UCzQUP1qoWDoEbmsQxvdjxgQ"),
    ("JRE Clips", "UCnxGkOGNMqQEUMvroOWps6Q"),
    ("All-In Podcast", "UCESLZhusAkFfsNsApnjF_Cg"),
    ("Lex Fridman", "UCSHZKyawb77ixDdsGog4iWA"),
    ("Theo Von", "UC5AQEUAwCh1sGDvkQtkDWUQ"),
    ("The Diary Of A CEO", "UCGq-a57w-aPwyi3pW7XLiHw"),
    ("Tucker Carlson", "UCGttrUON87gWfU6dMWm1fcA"),
    ("Shawn Ryan Show", "UCkoujZQZatbqy4KGcgjpVxQ"),
    ("The Tim Dillon Show", "UC4woSp8ITBoYDmjkukhEhxg"),
    ("Andrew Huberman", "UC2D2CMWXMOVWx7giW1n3LIg"),
    ("IMPAULSIVE", "UCGeBogGDZ9W3dsGx-mWQGJA"),
    ("FULL SEND PODCAST", "UChPuCAEXg7iYkVNjQY1NGYg"),
    ("PBD Podcast", "UCGX7nGXpz-CmO_Arg-cgJ7A"),
    ("Anthony Pompliano", "UCevXpeL8cNyAnww-NqJ4m2w"),
    ("20VC", "UCf0PBRjhf0rF8fWBIxTuoWA"),
    ("Breaking Points", "UCDRIjKy6eZOvKtOELtTdeUA"),
    # big news channels that post only a few times a day
    ("CNBC", "UCvJJ_dzjViJCoLf5uKUTwoA"),
    ("The Economist", "UC0p5jTq6Xx_DosDFxVXnWaQ"),
    ("The Wall Street Journal", "UCK7tptUDHh-RYDsdxO1-5QQ"),
    ("Financial Times", "UCoUxsWakJucWg46KW5RsvPw"),
    ("Business Insider", "UCcyq283he07B7_KUX07mmtA"),
    ("The New York Times", "UCqnbDFdCpuN8CMEg0VuEBqA"),
    ("POLITICO", "UCgjtvMmHXbutALaw9XzRkAg"),
    ("Washington Post", "UCHd62-u_v4DvJ8TCFtpi4GA"),
    ("Guardian News", "UCIRYBXDze5krPDzAEOxFGVA"),
    ("Face the Nation", "UC2x_qNZXKDxNP1j4jvHATRQ"),
    ("VICE News", "UCZaT_X_mc0BI-djXOlfhqWQ"),
    ("Vox", "UCLXo7UDZvByw2ixzpQCufnA"),
    ("Axios", "UCfU4-ArXuSX0tpyApyklMAg"),
    ("The Daily Wire", "UCaeO5vkdj5xOQHp4UmIN6dw"),
    ("The Daily Show", "UCwWhs_6x42TyRM4Wstoq8HA"),
    ("CNBC Make It", "UCH5_L3ytGbBziX0CLuYdQ1Q"),
]

# Each source: key, alert label, kind, url, options.
#   kind "rss"  : RSS/Atom feed. option "category" keeps only items in that category.
#   kind "sitemap": the site's sitemap.xml; option "path" as for "page". Titles are looked up
#                 from the post itself when it's new.
#   kind "page" : listing page. option "path" is the URL path prefix of individual posts;
#                 only links of the form <path><one-segment-slug> count.
# Several entries may share a key (e.g. a page and its feed); results are merged.
SOURCES = [
    # OpenAI: every news section. The feed misses some posts, so the news page and each
    # section's sitemap are read too.
    ("openai-all", "OpenAI post", "rss", "https://openai.com/news/rss.xml", {}),
    ("openai-all", "OpenAI post", "page", "https://openai.com/news/", {"path": "/index/"}),
    *[("openai-all", "OpenAI post", "sitemap", f"https://openai.com/sitemap.xml/{s}/", {"path": "/index/"})
      for s in ("company", "research", "product", "release", "milestone", "safety", "engineering",
                "science", "security", "intelligence-age", "global-affairs",
                "global-affairs-news-listed", "ai-adoption", "applied-ai")],
    ("openai-all", "OpenAI post", "sitemap", "https://openai.com/sitemap.xml/safety-and-alignment/",
     {"path": "/safety-alignment/"}),

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
    # Andrej Karpathy: public GitHub activity (new repos, pushes, releases), gists, and blog
    ("karpathy-github", "Karpathy GitHub", "gh_events",
     "https://api.github.com/users/karpathy/events/public?per_page=50", {}),
    ("karpathy-gist", "Karpathy gist", "rss", "https://gist.github.com/karpathy.atom", {}),
    ("karpathy-blog", "Karpathy blog post", "rss", "https://karpathy.github.io/feed.xml", {}),
] + [
    (YT_KEY, "YouTube channels (transcript keywords)", "yt_channel",
     f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", {})
    for _name, cid in YT_CHANNELS
]


def fetch(url):
    ua = SEC_UA if "sec.gov" in url else BROWSER_UA
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    }
    if url.startswith("https://api.github.com/"):
        headers["Accept"] = "application/vnd.github+json"
        if GH_TOKEN:
            headers["Authorization"] = f"Bearer {GH_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
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
            elif kind == "gh_events":
                items = parse_gh_events(body)
            elif kind == "gh_gists":
                items = parse_gh_gists(body)
            elif kind == "yt_channel":
                items = parse_yt_feed(body)
            elif kind == "rss":
                items = parse_feed(body, opts.get("category"))
            elif kind == "sitemap":
                items = parse_sitemap(body, url, opts["path"])
            else:
                items = parse_page(body, url, opts["path"])
            if items or kind in ("gh_events", "gh_gists"):  # quiet stretches are normal on GitHub
                ok_keys.add(key)
            else:
                problems.setdefault(key, []).append(f"{url}: no posts found")
            for it in items:
                # "id" lets different sites report the same item once (e.g. one Record issue)
                ident = it.get("id") or it["url"]
                if key == "openai-all":  # OpenAI's feed drops the trailing slash its sitemaps use
                    ident = ident.rstrip("/")
                prev = found.get(ident)
                if prev is None or (prev.get("needs_title") and not it.get("needs_title")):
                    found[ident] = {**it, "source": key, "label": label}
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


def parse_gh_events(body):
    """Public GitHub activity worth knowing about: new repos, pushes, releases, repos made public."""
    items = []
    for e in json.loads(body):
        repo = (e.get("repo") or {}).get("name", "")
        p = e.get("payload") or {}
        kind = e.get("type")
        if kind == "PushEvent":
            commits = p.get("commits") or []
            msg = (commits[-1].get("message", "").splitlines() or [""])[0] if commits else ""
            title, url = f"Pushed to {repo}" + (f": {msg}" if msg else ""), f"https://github.com/{repo}/commits"
        elif kind == "CreateEvent" and p.get("ref_type") == "repository":
            desc = p.get("description") or ""
            title, url = f"New repo: {repo}" + (f" — {desc}" if desc else ""), f"https://github.com/{repo}"
        elif kind == "PublicEvent":
            title, url = f"Made public: {repo}", f"https://github.com/{repo}"
        elif kind == "ReleaseEvent":
            rel = p.get("release") or {}
            title = f"Release in {repo}: {rel.get('name') or rel.get('tag_name') or ''}".rstrip(": ")
            url = rel.get("html_url") or f"https://github.com/{repo}/releases"
        else:
            continue  # stars, follows, comments etc. are skipped
        items.append({"id": f"ghe:{e.get('id')}", "title": title, "url": url})
    return items


def parse_gh_gists(body):
    items = []
    for g in json.loads(body):
        files = list((g.get("files") or {}).keys())
        title = g.get("description") or (files[0] if files else "New gist")
        items.append({"id": f"gist:{g.get('id')}", "title": title, "url": g.get("html_url", "")})
    return items


def parse_yt_feed(body):
    """A channel's upload feed (latest 15 videos)."""
    atom, yt = "{http://www.w3.org/2005/Atom}", "{http://www.youtube.com/xml/schemas/2015}"
    items = []
    for e in ET.fromstring(body).iter(atom + "entry"):
        vid = (e.findtext(yt + "videoId") or "").strip()
        if not vid:
            continue
        items.append({"id": f"ytv:{vid}", "vid": vid, "title": clean(e.findtext(atom + "title")),
                      "url": f"https://www.youtube.com/watch?v={vid}",
                      "channel": clean(e.findtext(f"{atom}author/{atom}name")),
                      "channel_id": (e.findtext(yt + "channelId") or "").strip()})
    return items


def load_keywords():
    """keywords.txt: one word or phrase per line; lines starting with # are notes."""
    if not KEYWORDS_FILE.exists():
        return []
    words = []
    for line in KEYWORDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.append(line.lower())
    return words


def kw_pattern(kw):
    # whole word or phrase, also matching plurals (memecoin -> memecoins); any spacing
    body = r"\s+".join(re.escape(p) for p in kw.split())
    return re.compile(r"(?<![a-z0-9])" + body + r"(?:s|es)?(?![a-z0-9])", re.I)


def find_keywords(segments, title, keywords):
    """Return {keyword: (count, start_seconds or None, snippet)} for keywords that were said."""
    texts = [clean(s.get("text", "")) for s in segments]
    starts = [float(s.get("start") or 0) for s in segments]
    full = " ".join(texts)
    hits = {}
    for kw in keywords:
        pat = kw_pattern(kw)
        count = len(pat.findall(full))
        if not count:
            if pat.search(title or ""):
                hits[kw] = (0, None, "in the title")
            continue
        # first line where it's said (a phrase may run onto the next line)
        for i, t in enumerate(texts):
            window = t + (" " + texts[i + 1] if i + 1 < len(texts) else "")
            if pat.search(window):
                snippet = " ".join(texts[max(0, i - 1): i + 2])
                if len(snippet) > 220:
                    snippet = snippet[:217] + "..."
                hits[kw] = (count, starts[i], snippet)
                break
    return hits


def apify_transcripts(urls):
    """Run the Apify transcript tool on these videos and return its results."""
    data = json.dumps({"urls": [{"url": u} for u in urls], "languages": ["en"],
                       "outputFormat": "json"}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items?timeout=240",
        data=data, headers={"Content-Type": "application/json",
                            "Authorization": f"Bearer {APIFY_TOKEN}"})
    with urllib.request.urlopen(req, timeout=290) as resp:
        return json.loads(resp.read().decode("utf-8"))


def clock(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def process_youtube(meta, warned, now_dt):
    """Fetch transcripts for waiting videos and ping on keyword hits."""
    pending = meta.setdefault("yt_pending", {})
    keywords = load_keywords()
    due = [v for v, p in pending.items() if datetime.fromisoformat(p["next"]) <= now_dt]
    if not due:
        return
    if not APIFY_TOKEN:
        print("WARN APIFY_TOKEN secret is not set; skipping transcripts")
        return
    batch = due[:YT_BATCH]
    try:
        results = apify_transcripts([pending[v]["url"] for v in batch])
    except urllib.error.HTTPError as exc:
        print("WARN Apify:", exc.code, exc.reason)
        if exc.code in (401, 402, 403) and "apify" not in warned:
            why = {401: "the Apify key was rejected (expired or deleted?)",
                   402: "the Apify account is out of credit for this month",
                   403: "the Apify key isn't allowed to run the transcript tool"}[exc.code]
            notify("Can't get YouTube transcripts", f"Transcript keyword alerts are paused: {why}.",
                   tags="warning")
            warned.add("apify")
        return
    except Exception as exc:  # network hiccup or timeout: try again next check
        print("WARN Apify:", exc)
        return
    warned.discard("apify")

    by_vid = {}
    for item in results if isinstance(results, list) else []:
        m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", (item.get("videoUrl") or "") + " " + (item.get("inputUrl") or ""))
        if m and item.get("transcript"):
            by_vid[m.group(1)] = item["transcript"]

    for vid in batch:
        p = pending[vid]
        segs = by_vid.get(vid)
        if not segs:  # no captions yet: wait and try again, then give up
            if p["tries"] >= len(YT_RETRY_MINUTES):
                print("GAVE UP (no transcript)", vid)
                del pending[vid]
            else:
                p["next"] = (now_dt + timedelta(minutes=YT_RETRY_MINUTES[p["tries"]])).isoformat(timespec="seconds")
                p["tries"] += 1
            continue
        del pending[vid]
        hits = find_keywords(segs, p["title"], keywords)
        print("TRANSCRIPT", vid, "hits:", sorted(hits))
        if not hits:
            continue
        first_kw = min(hits, key=lambda k: (hits[k][1] is None, hits[k][1] or 0))
        count, start, snippet = hits[first_kw]
        said = ", ".join(f'"{k}"' + (f" x{hits[k][0]}" if hits[k][0] > 1 else "") for k in hits)
        where = f" at {clock(start)}" if start is not None else ""
        msg = (f'"{snippet}"' if start is not None else "Keyword is in the title.") + f"{where}\n\n{p['title']}"
        click = p["url"] + (f"&t={int(start)}s" if start is not None else "")
        notify(f"{said} said on {p['channel']}", msg, click=click, tags="arrow_forward", icon=YT_ICON)


def notify(title, message, click=None, tags="green_circle", icon=None):
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message}
    if tags:
        payload["tags"] = [tags]
    if icon:
        payload["icon"] = icon
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
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    for key, msgs in failing.items():
        for m in msgs:
            print("WARN", m)

    # YouTube: a channel read for the first time (e.g. newly added) has its existing videos
    # recorded quietly, so only later uploads get their transcripts checked
    yt_channels = set(meta.get("yt_channels", []))
    for k, it in found.items():
        if (it["source"] == YT_KEY and YT_KEY in seeded
                and it.get("channel_id") not in yt_channels and k not in seen):
            seen[k] = {"title": it["title"], "source": YT_KEY, "first_seen": now}
    yt_channels |= {it.get("channel_id") for it in found.values() if it["source"] == YT_KEY}
    meta["yt_channels"] = sorted(c for c in yt_channels if c)

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
        if it["source"] == YT_KEY:  # new upload: queue it for a transcript check, no ping yet
            meta.setdefault("yt_pending", {})[it["vid"]] = {
                "title": it["title"], "channel": it["channel"], "url": it["url"],
                "tries": 0, "next": now}
            seen[k] = {"title": it["title"], "source": YT_KEY, "first_seen": now}
            print("QUEUED", k)
            continue
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

    # 4. transcripts for queued YouTube videos (keyword pings)
    process_youtube(meta, warned, now_dt)

    seen[META_KEY] = {**meta, "seeded": sorted(seeded), "warned": sorted(warned)}
    SEEN_FILE.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n")
    print(f"checked {len(found)} items, {len(new)} new, {len(new_sources)} sources added, "
          f"{len(failing)} sources failing")


if __name__ == "__main__":
    main()
