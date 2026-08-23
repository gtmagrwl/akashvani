import re
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import os
import subprocess
import PyRSS2Gen
import time
import urllib
import sys

# --- CONFIGURATION ---
REPO_PATH = "/Users/gtmagrwl/akashvani"
GITHUB_USER = "gtmagrwl"
REPO_NAME = "akashvani"
BASE_URL = f"https://{GITHUB_USER}.github.io/{REPO_NAME}"
# The old admin-ajax endpoint is dead: newsonair.gov.in now 403s every POST
# request (their own Audio Archive Search page is broken by it too). Everything
# below is plain GET against server-rendered pages.
AJAX_URL = "https://newsonair.gov.in/wp-admin/admin-ajax.php"   # kept for reference; unused

# Server-rendered listing of Hindi morning-news bulletins. One <table> of
# <tr><td>title</td><td>23 Aug 2026</td><td>8.00 AM</td><td><audio><source src=..>
# Paginated with ?page=N, 10 rows per page.
LISTEN_URL = "https://newsonair.gov.in/listen-news-category/morning-news-hi/"

# Transcript detail pages have sequential slugs and expose no date of their own,
# so we anchor a known slug number to a known date and count from there.
TRANSCRIPT_BASE = ("https://newsonair.gov.in/bulletins-detail/"
                   + urllib.parse.quote("समाचार-प्रभात") + "-{n}/")
ANCHOR_FILE = os.path.join(REPO_PATH, ".transcript_anchor")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:148.0) Gecko/20100101 Firefox/148.0",
    "Referer": "https://www.newsonair.gov.in/audio-archive-search/",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
}

# Headers used when hitting the bulletin detail / transcript endpoints
# Plain browser-style GET headers. These pages are ordinary HTML now; sending
# X-Requested-With / a form Content-Type (left over from the old ajax POST) makes
# the request look like the blocked ajax traffic and it comes back unusable.
DETAIL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:148.0) Gecko/20100101 Firefox/148.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9,hi;q=0.8",
    "Referer": "https://newsonair.gov.in/",
}

PODCAST_NS = "https://podcastindex.org/namespace/1.0"

# Compact git history once the object store grows past this. Every MP3 ever
# committed lives in history forever, so without this the repo grows ~180 MB a
# month and eventually breaks GitHub Pages (1 GB limit).
MAX_REPO_MB = 400

# Tracks consecutive fetch failures so a silent outage can't go unnoticed again.
STATE_FILE = os.path.join(REPO_PATH, ".fetch_state")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    """Timestamped print so logs are easier to debug."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def notify(title, message):
    """Raise a macOS notification so failures surface instead of dying in the log."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification {message!r} with title {title!r}'],
            check=False, timeout=10
        )
    except Exception as e:
        log(f"(could not send notification: {e})")


def record_failure(reason):
    """Count consecutive failed runs and alert once the problem looks persistent."""
    try:
        n = int(open(STATE_FILE).read().strip() or 0)
    except Exception:
        n = 0
    n += 1
    try:
        with open(STATE_FILE, "w") as f:
            f.write(str(n))
    except Exception:
        pass
    log(f"Consecutive failed runs: {n}")
    if n in (2, 7) or n % 14 == 0:
        notify("Akashvani feed stalled",
               f"{n} runs in a row have failed: {reason}")
    return n


def record_success():
    try:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

def cleanup_old_files(days=14):
    """Delete MP3 and transcript files older than `days` days to stay under GitHub limits."""
    now = time.time()
    cutoff = now - (days * 86400)
    for f in os.listdir(REPO_PATH):
        if f.endswith((".mp3", ".txt", ".vtt", ".json", ".srt")):
            f_path = os.path.join(REPO_PATH, f)
            if os.path.getmtime(f_path) < cutoff:
                log(f"Deleting old file: {f}")
                os.remove(f_path)


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def _parse_item_date(date_str):
    """Parse a newsonair date string (e.g. '19 March 2026') into a datetime."""
    date_str = date_str.strip()
    for fmt in ("%d %B %Y", "%d %B, %Y", "%d-%m-%Y",
                "%B %d, %Y", "%d %b %Y", "%d %b, %Y"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return datetime.min   # fallback: sorts to oldest


# ---------------------------------------------------------------------------
# Audio feed helpers
# ---------------------------------------------------------------------------

def _fmt_date(dt):
    """Match the existing archive's filename style: '5 July 2026', not '05 July 2026'."""
    return f"{dt.day} {dt:%B} {dt.year}"


def _get(url, headers=None, timeout=30):
    r = requests.get(url, headers=headers or HEADERS, timeout=timeout)
    r.raise_for_status()
    return r


def _parse_listing_page(html):
    """Pull (title, date, audio_url) out of one listing page's table."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []
    body = table.find("tbody") or table
    out = []
    for tr in body.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        source = tds[3].find("source")
        if not source or not source.get("src"):
            continue
        dt = _parse_item_date(tds[1].get_text(strip=True))
        if dt == datetime.min:
            continue
        out.append({
            "title": tds[0].get_text(strip=True),
            "date": _fmt_date(dt),
            "audio_url": source["src"],
            "_dt": dt,
        })
    return out


def get_news_items(lookback_days=14, max_pages=6):
    """
    Fetch recent bulletins from the server-rendered listing page.

    Replaces the old admin-ajax POST, which the site now blocks outright.
    Walks pages newest-first and stops once it goes past the lookback window.
    """
    cutoff = datetime.now() - timedelta(days=lookback_days)
    items, page = [], 1

    while page <= max_pages:
        url = LISTEN_URL if page == 1 else f"{LISTEN_URL}?page={page}"
        html = None
        for attempt in range(1, 4):
            try:
                log(f"Fetching listing page {page} (attempt {attempt}/3)...")
                html = _get(url).text
                break
            except Exception as e:
                log(f"ERROR fetching page {page} (attempt {attempt}): {e}")
                if attempt < 3:
                    time.sleep(10)
        if html is None:
            log(f"FAILED: could not fetch listing page {page}.")
            break

        rows = _parse_listing_page(html)
        if not rows:
            log(f"No bulletin rows on page {page} - stopping.")
            break

        past_window = False
        for row in rows:
            if row["_dt"] < cutoff:
                past_window = True
                break
            items.append(row)

        if past_window:
            break
        page += 1

    for it in items:
        it.pop("_dt", None)
    log(f"Found {len(items)} news items.")
    return items


def download_audio(items):
    """Download MP3s that don't already exist locally."""
    os.makedirs(REPO_PATH, exist_ok=True)
    downloaded = 0
    for item in items:
        filename = f"{item['date'].replace(' ', '_')}_{item['title']}.mp3"
        filepath = os.path.join(REPO_PATH, filename)
        if not os.path.exists(filepath):
            try:
                log(f"Downloading: {filename}")
                res = requests.get(item['audio_url'], timeout=60)
                res.raise_for_status()
                with open(filepath, 'wb') as f:
                    f.write(res.content)
                downloaded += 1
            except Exception as e:
                log(f"ERROR downloading {filename}: {e}")
    log(f"Downloaded {downloaded} new file(s).")
    return items


# ---------------------------------------------------------------------------
# Transcript helpers
# ---------------------------------------------------------------------------

def _load_anchor():
    """Anchor maps a known transcript slug number to a known bulletin date."""
    try:
        import json
        with open(ANCHOR_FILE) as f:
            a = json.load(f)
        return int(a["n"]), datetime.strptime(a["date"], "%Y-%m-%d")
    except Exception:
        return None, None


def _save_anchor(n, dt):
    try:
        import json
        with open(ANCHOR_FILE, "w") as f:
            json.dump({"n": int(n), "date": dt.strftime("%Y-%m-%d")}, f)
    except Exception as e:
        log(f"(could not save transcript anchor: {e})")


def _detail_exists(n):
    try:
        r = requests.get(TRANSCRIPT_BASE.format(n=n), headers=DETAIL_HEADERS, timeout=20)
        ok = r.status_code == 200 and "entry-content" in r.text
        if not ok:
            log(f"  detail slug {n}: HTTP {r.status_code}, "
                f"entry-content={'yes' if 'entry-content' in r.text else 'no'} "
                f"({len(r.text)} bytes)")
        return ok
    except Exception as e:
        log(f"  detail slug {n}: request failed: {e}")
        return False


def refresh_anchor(newest_date):
    """
    Walk the sequential transcript slugs upward to find the newest one and pin
    it to the newest bulletin date. Detail pages carry no date of their own, so
    this anchor is the only way to line slugs up with dates.
    """
    n, anchor_date = _load_anchor()
    if n is None:
        log("No transcript anchor stored - skipping transcripts this run.")
        return None, None
    if not _detail_exists(n):
        log(f"Transcript anchor slug {n} is unreachable - leaving the anchor "
            f"untouched and skipping transcripts this run.")
        return None, None

    highest = n
    for candidate in range(n + 1, n + 16):
        if _detail_exists(candidate):
            highest = candidate
        else:
            break

    if highest == n and newest_date.date() > anchor_date.date():
        # Newer bulletins exist but no newer slug was found: the probe is not
        # working. Re-pinning the old slug to a new date would misdate every
        # transcript from here on, so bail out instead.
        log(f"Transcript slug did not advance past {n} despite a newer bulletin "
            f"({newest_date:%d %B %Y}) - not re-anchoring.")
        return None, None

    _save_anchor(highest, newest_date)
    log(f"Transcript anchor: slug {highest} = {newest_date:%d %B %Y}")
    return highest, newest_date


def fetch_bulletin_detail_url(target_date: datetime) -> str | None:
    """Return the transcript detail URL for a bulletin on target_date, or None."""
    n, anchor_date = _load_anchor()
    if n is None:
        return None
    offset = (target_date.date() - anchor_date.date()).days
    candidate = n + offset
    if candidate < 1 or abs(offset) > 400:
        return None
    url = TRANSCRIPT_BASE.format(n=candidate)
    try:
        r = requests.get(url, headers=DETAIL_HEADERS, timeout=20)
        if r.status_code == 200 and "entry-content" in r.text:
            return url
        log(f"  {target_date:%d %B %Y} -> slug {candidate}: HTTP {r.status_code}, "
            f"entry-content={'yes' if 'entry-content' in r.text else 'no'} "
            f"({len(r.text)} bytes)")
    except Exception as e:
        log(f"ERROR checking detail URL for {target_date:%d %B %Y}: {e}")
    return None


def fetch_transcript(url: str) -> str:
    """Fetch and return the clean Hindi text from a bulletin detail page."""
    r = requests.get(
        url,
        headers=DETAIL_HEADERS,
        timeout=20,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    content = soup.select_one(".entry-content")
    if not content:
        raise RuntimeError(f"Could not find .entry-content on {url}")
    paras = [
        p.get_text(separator=" ", strip=True)
        for p in content.find_all("p")
        if len(p.get_text(strip=True)) > 10
    ]
    return "\n\n".join(paras)


def generate_transcripts(items):
    """
    For each item that has a local MP3 but no transcript yet, fetch the
    official Hindi text from the newsonair bulletin detail page and save it
    as a plain-text .txt file.  Already-existing .txt files are skipped
    (idempotent).

    Plain text (text/plain) is the simplest, most universally supported
    transcript format.  It won't time-sync with playback but works reliably
    in Apple Podcasts, Pocket Casts, and any other Podcasting 2.0 client.
    The text is the official newsonair transcript, so it is accurate Hindi.
    """
    log("--- Fetching transcripts ---")
    dated = [i for i in items if _parse_item_date(i["date"]) != datetime.min]
    if not dated:
        return
    refresh_anchor(max(_parse_item_date(i["date"]) for i in dated))
    for item in items:
        filename = f"{item['date'].replace(' ', '_')}_{item['title']}.mp3"
        mp3_path = os.path.join(REPO_PATH, filename)
        if not os.path.exists(mp3_path):
            continue  # audio not downloaded yet, skip

        txt_path = mp3_path.replace(".mp3", ".txt")
        if os.path.exists(txt_path):
            log(f"Transcript already exists, skipping: {os.path.basename(txt_path)}")
            continue

        try:
            target_date = _parse_item_date(item["date"])
        except Exception:
            log(f"Cannot parse date '{item['date']}' — skipping transcript")
            continue

        log(f"Fetching transcript for: {item['date']}")
        detail_url = fetch_bulletin_detail_url(target_date)
        if not detail_url:
            log(f"No detail page URL found for {item['date']} — skipping")
            continue

        try:
            text = fetch_transcript(detail_url)
        except Exception as e:
            log(f"ERROR fetching transcript: {e} — skipping")
            continue

        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text)
        log(f"✓ Transcript saved: {os.path.basename(txt_path)} ({len(text)} chars)")


# ---------------------------------------------------------------------------
# RSS feed generation
# ---------------------------------------------------------------------------

def generate_rss(items):
    """Creates feed.xml with encoded URLs, newest episode first."""
    items = sorted(items, key=lambda x: _parse_item_date(x['date']), reverse=True)
    rss_items = []
    for item in items:
        filename = f"{item['date'].replace(' ', '_')}_{item['title']}.mp3"
        local_path = os.path.join(REPO_PATH, filename)
        if os.path.exists(local_path):
            encoded_filename = urllib.parse.quote(filename)
            public_url = f"{BASE_URL}/{encoded_filename}"
            rss_items.append(PyRSS2Gen.RSSItem(
                title=f"{item['title']} - {item['date']}",
                link=public_url,
                description="Daily Audio Bulletin from News on Air",
                guid=PyRSS2Gen.Guid(public_url),
                pubDate=_parse_item_date(item['date']),
                enclosure=PyRSS2Gen.Enclosure(
                    public_url,
                    str(os.path.getsize(local_path)),
                    "audio/mpeg"
                )
            ))

    rss = PyRSS2Gen.RSS2(
        title="Akashvani Hindi News",
        link=BASE_URL,
        description="Daily News on Air Bulletins",
        lastBuildDate=datetime.now(),
        items=rss_items,
        image=PyRSS2Gen.Image(f"{BASE_URL}/logo.png", "Akashvani", BASE_URL)
    )
    feed_path = os.path.join(REPO_PATH, "feed.xml")
    with open(feed_path, "w", encoding='utf-8') as f:
        rss.write_xml(f, encoding='utf-8')

    # PyRSS2Gen doesn't support custom namespaces, so post-process to add
    # xmlns:itunes and channel-level iTunes metadata required by Apple Podcasts
    with open(feed_path, encoding='utf-8') as f:
        content = f.read()
    content = re.sub(
        r'(<rss\b[^>]*)(>)',
        r'\1 xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"\2',
        content, count=1
    )
    ITUNES_CHANNEL = (
        '<language>hi</language>'
        '<itunes:author>Akashvani / All India Radio</itunes:author>'
        '<itunes:explicit>false</itunes:explicit>'
        '<itunes:category text="News"/>'
        f'<itunes:image href="{BASE_URL}/logo.png"/>'
    )
    content = re.sub(
        r'(<description>Daily News on Air Bulletins</description>)',
        r'\1' + ITUNES_CHANNEL,
        content, count=1
    )
    with open(feed_path, "w", encoding='utf-8') as f:
        f.write(content)
    log("✓ feed.xml written")


def add_transcript_tags_to_feed():
    """
    Post-process feed.xml to inject a <podcast:transcript type="text/plain">
    element for every episode that has a local .txt transcript file.

    Plain text is the simplest format: no timestamps, no special syntax,
    works in every Podcasting 2.0 client.  The official newsonair Hindi text
    is accurate and requires no local processing to produce.
    """
    feed_path = os.path.join(REPO_PATH, "feed.xml")
    with open(feed_path, encoding="utf-8") as f:
        content = f.read()

    # Add podcast namespace to <rss> opening tag (idempotent guard)
    if f'xmlns:podcast="{PODCAST_NS}"' not in content:
        content = re.sub(
            r'(<rss\b[^>]*)(>)',
            lambda m: m.group(1) + f' xmlns:podcast="{PODCAST_NS}"' + m.group(2),
            content,
            count=1,
        )

    injected = 0

    def _inject(match):
        nonlocal injected
        item_block = match.group(0)
        guid_m = re.search(r'<guid[^>]*>([^<]+)</guid>', item_block)
        if not guid_m:
            return item_block
        guid_url = guid_m.group(1)  # percent-encoded MP3 URL

        txt_url      = re.sub(r'\.mp3$', '.txt', guid_url)
        txt_filename = urllib.parse.unquote(txt_url.replace(BASE_URL + '/', ''))
        if os.path.exists(os.path.join(REPO_PATH, txt_filename)):
            tag = (
                f'    <podcast:transcript url="{txt_url}" '
                f'type="text/plain" language="hi"/>\n  '
            )
            item_block = item_block.replace('</item>', tag + '</item>', 1)
            injected += 1

        return item_block

    content = re.sub(r'<item>.*?</item>', _inject, content, flags=re.DOTALL)

    with open(feed_path, "w", encoding="utf-8") as f:
        f.write(content)

    log(f"✓ Podcast transcript tags injected: {injected} item(s) updated in feed.xml")


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def _repo_size_mb():
    """Size of the git object store in MB (loose objects + packs)."""
    out = subprocess.run(["git", "count-objects", "-v"],
                         capture_output=True, text=True).stdout
    kb = 0
    for line in out.splitlines():
        if line.startswith("size:") or line.startswith("size-pack:"):
            try:
                kb += int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    return kb / 1024


def compact_history():
    """
    Collapse the whole repo into a single root commit and force-push it.

    Deleting old MP3s from the working tree does NOT shrink the repo, because
    every blob stays reachable through history. This throws the history away
    so the remote size tracks the working tree instead of growing forever.
    """
    before = _repo_size_mb()
    log(f"Repo is {before:.0f} MB (limit {MAX_REPO_MB} MB) - compacting history...")
    try:
        subprocess.run(["git", "checkout", "--orphan", "_compact"], check=True)
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-m",
                        f"Akashvani archive (compacted {datetime.now():%Y-%m-%d})"],
                       check=True)
        subprocess.run(["git", "branch", "-M", "_compact", "main"], check=True)
        subprocess.run(["git", "push", "origin", "main", "--force"], check=True)
        subprocess.run(["git", "reflog", "expire", "--expire=now", "--all"], check=True)
        subprocess.run(["git", "gc", "--prune=now"], check=True)
        log(f"Compaction complete: {before:.0f} MB -> {_repo_size_mb():.0f} MB")
    except subprocess.CalledProcessError as e:
        log(f"Compaction failed (exit {e.returncode}): {e.cmd}")
        notify("Akashvani", "Git history compaction failed - see cron_log.txt")


def push_to_github():
    """Commit and push new files to GitHub, compacting history when it gets large."""
    try:
        os.chdir(REPO_PATH)
        log("Syncing with GitHub...")
        # NOTE: deliberately no `git reset --mixed origin/main` here. The remote
        # is force-pushed from this machine, so local main is authoritative;
        # resetting to origin would resurrect history we just compacted away.
        subprocess.run(["git", "add", "."], check=True)
        status = subprocess.run(["git", "status", "--porcelain"],
                                capture_output=True, text=True)
        if status.stdout.strip():
            subprocess.run([
                "git", "commit", "-m",
                f"News Update {datetime.now().strftime('%Y-%m-%d')}"
            ], check=True)
            subprocess.run(["git", "push", "origin", "main", "--force"], check=True)
            log("--- GitHub Updated Successfully ---")
        else:
            log("No new changes detected.")

        if _repo_size_mb() > MAX_REPO_MB:
            compact_history()
    except subprocess.CalledProcessError as e:
        log(f"Git error (exit code {e.returncode}): {e.cmd}")
        notify("Akashvani", "git push failed - see cron_log.txt")
    except Exception as e:
        log(f"Git error: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log("=" * 50)
    log("Starting Akashvani Update")
    log("=" * 50)

    news_items = get_news_items(lookback_days=14)
    if news_items:
        record_success()
        download_audio(news_items)
        cleanup_old_files(days=14)
        generate_transcripts(news_items)
        generate_rss(news_items)
        add_transcript_tags_to_feed()
        push_to_github()
    else:
        log("No news items found — skipping download and push.")
        record_failure("newsonair.gov.in returned no bulletins (see errors above)")

    log("Done.")
