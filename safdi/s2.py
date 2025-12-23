import os
import re
import json
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------- CONFIG ----------------
PODCAST_ID = "1447749859"
OUT_DIR = Path("transcripts")
SLEEP_SEC = 0.25
# ---------------------------------------


APPLE_LOOKUP_URL = "https://itunes.apple.com/lookup?id=" + PODCAST_ID


def http_get(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def fetch_feed_url() -> str:
    data = json.loads(http_get(APPLE_LOOKUP_URL))
    results = data.get("results", [])
    if not results:
        raise RuntimeError("Apple lookup returned no results")
    feed_url = results[0].get("feedUrl")
    if not feed_url:
        raise RuntimeError("feedUrl missing from Apple lookup response")
    return feed_url


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def find_episode_number(item, fallback: int) -> int:
    ep = item.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}episode")
    if ep is not None and ep.text and ep.text.isdigit():
        return int(ep.text)
    return fallback


def find_transcript_urls(item):
    urls = []

    # Podcasting 2.0 transcript tag
    for el in item.findall(".//{*}transcript"):
        url = el.attrib.get("url")
        if url:
            urls.append(url)

    # Scan enclosure-like links
    for el in item.findall(".//{*}link"):
        href = el.attrib.get("href", "")
        if "transcript" in href.lower():
            urls.append(href)

    # Deduplicate
    seen = set()
    clean = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            clean.append(u)
    return clean


def fetch_transcript_text(url: str) -> str | None:
    try:
        raw = http_get(url)
        text = raw.decode("utf-8", errors="ignore")

        # JSON transcript
        if url.lower().endswith(".json"):
            data = json.loads(text)
            if isinstance(data, dict):
                if "text" in data:
                    return data["text"]
                if "segments" in data:
                    return "\n".join(
                        seg.get("text", "") for seg in data["segments"]
                        if isinstance(seg, dict)
                    )
            return json.dumps(data, indent=2)

        # Plain text / HTML
        return strip_html(text)

    except Exception:
        return None


def main():
    OUT_DIR.mkdir(exist_ok=True)

    print("Fetching RSS feed URL…")
    feed_url = fetch_feed_url()
    print("RSS:", feed_url)

    rss_xml = http_get(feed_url)
    root = ET.fromstring(rss_xml)

    items = root.findall(".//item")
    if not items:
        raise RuntimeError("No episodes found in RSS")

    # Oldest → newest for sane numbering
    items.reverse()

    for idx, item in enumerate(items, start=1):
        title_el = item.find("title")
        title = strip_html(title_el.text if title_el is not None else "")
        ep_num = find_episode_number(item, idx)
        fname = f"{ep_num:03d}.txt"
        out_path = OUT_DIR / fname

        if out_path.exists():
            print(f"[skip] {fname}")
            continue

        print(f"\nEpisode {ep_num:03d}: {title}")

        transcript_text = None
        transcript_urls = find_transcript_urls(item)

        for url in transcript_urls:
            transcript_text = fetch_transcript_text(url)
            if transcript_text:
                print(f"[ok] transcript downloaded")
                break

        if not transcript_text:
            print("[no transcript available]")
            transcript_text = "(No published transcript available for this episode.)"

        header = f"{title}\nEpisode: {ep_num:03d}\n\n" + "-" * 60 + "\n\n"
        out_path.write_text(header + transcript_text, encoding="utf-8")
        print(f"[saved] {out_path}")

        time.sleep(SLEEP_SEC)

    print("\nDone.")


if __name__ == "__main__":
    main()
