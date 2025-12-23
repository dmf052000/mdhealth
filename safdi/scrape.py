import os
import re
import json
import time
import shutil
import requests
import feedparser
from pathlib import Path
from slugify import slugify
from tqdm import tqdm

# ---------- CONFIG ----------
PODCAST_ID = "1447749859"
OUT_DIR = Path("transcripts")
AUDIO_DIR = Path("audio_tmp")
MODEL_SIZE = "small"  # faster-whisper model: tiny/base/small/medium/large-v3
LANGUAGE = "en"       # set None for auto-detect
REQUEST_TIMEOUT = 60
SLEEP_BETWEEN_EPISODES_SEC = 0.25  # be nice to hosts
# ---------------------------


APPLE_LOOKUP = "https://itunes.apple.com/lookup"


def fetch_feed_url(itunes_id: str) -> str:
    r = requests.get(APPLE_LOOKUP, params={"id": itunes_id}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if not data.get("results"):
        raise RuntimeError("No results from Apple lookup API. Check podcast ID.")
    feed_url = data["results"][0].get("feedUrl")
    if not feed_url:
        raise RuntimeError("feedUrl missing from lookup response (show may be restricted).")
    return feed_url


def safe_episode_number(entry, fallback_index: int) -> int:
    """
    Prefer itunes:episode when present. Otherwise use a sequential fallback index (1..N).
    feedparser sometimes stores itunes_episode as 'itunes_episode'.
    """
    for key in ("itunes_episode", "itunes:episode", "episode"):
        val = entry.get(key)
        if val:
            try:
                return int(str(val).strip())
            except ValueError:
                pass
    return fallback_index


def find_transcript_urls(entry):
    """
    Tries to discover Podcasting 2.0 transcript URLs.
    Some feeds expose them in extensions; feedparser may store in entry['podcast_transcript'] or similar.
    We'll also scan raw content for 'transcript' links if present.
    Returns list of candidate URLs (best-first).
    """
    urls = []

    # Common patterns in feedparser results
    for key in ("podcast_transcript", "podcast:transcript", "transcript"):
        val = entry.get(key)
        if not val:
            continue
        # Sometimes it's a list of dicts
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    u = item.get("url") or item.get("href")
                    if u:
                        urls.append(u)
                elif isinstance(item, str):
                    urls.append(item)
        elif isinstance(val, dict):
            u = val.get("url") or val.get("href")
            if u:
                urls.append(u)
        elif isinstance(val, str):
            urls.append(val)

    # Scan links for anything transcript-like
    for l in entry.get("links", []):
        href = l.get("href")
        if not href:
            continue
        typ = (l.get("type") or "").lower()
        rel = (l.get("rel") or "").lower()
        if "transcript" in href.lower() or "text" in typ or "json" in typ:
            urls.append(href)
        if rel == "transcript":
            urls.append(href)

    # De-dupe while preserving order
    deduped = []
    seen = set()
    for u in urls:
        if u not in seen:
            deduped.append(u)
            seen.add(u)
    return deduped


def get_audio_url(entry):
    # enclosure is usually the actual audio
    if entry.get("enclosures"):
        href = entry["enclosures"][0].get("href")
        if href:
            return href

    for l in entry.get("links", []):
        if (l.get("rel") == "enclosure") and l.get("href"):
            return l["href"]

    return None


def download_file(url: str, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=REQUEST_TIMEOUT) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        with open(dest, "wb") as f, tqdm(
            total=total, unit="B", unit_scale=True, desc=f"Downloading {dest.name}"
        ) as pbar:
            for chunk in r.iter_content(chunk_size=1024 * 512):
                if chunk:
                    f.write(chunk)
                    pbar.update(len(chunk))


def fetch_transcript_text(url: str) -> str | None:
    """
    Attempts to download transcript. Supports:
    - plain text / html (we'll return raw text)
    - JSON (common for Podcasting 2.0) where it might include 'segments' or 'text'
    """
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        ct = (r.headers.get("content-type") or "").lower()

        # JSON transcript formats
        if "application/json" in ct or url.lower().endswith(".json"):
            data = r.json()
            # Try common structures
            if isinstance(data, dict):
                if "text" in data and isinstance(data["text"], str):
                    return data["text"]
                if "segments" in data and isinstance(data["segments"], list):
                    parts = []
                    for seg in data["segments"]:
                        if isinstance(seg, dict) and seg.get("text"):
                            parts.append(str(seg["text"]))
                    if parts:
                        return "\n".join(parts)
                # Some formats: { "results": { "channels": [ { "alternatives": [ { "transcript": "..." } ] } ] } }
                try:
                    alt = data["results"]["channels"][0]["alternatives"][0]["transcript"]
                    if isinstance(alt, str) and alt.strip():
                        return alt
                except Exception:
                    pass

            # Fallback: dump json
            return json.dumps(data, ensure_ascii=False, indent=2)

        # Plain text / html
        return r.text

    except Exception:
        return None


def transcribe_with_faster_whisper(audio_path: Path) -> str:
    from faster_whisper import WhisperModel

    model = WhisperModel(MODEL_SIZE, device="auto", compute_type="auto")
    segments, info = model.transcribe(
        str(audio_path),
        language=LANGUAGE,
        vad_filter=True,
        beam_size=5,
    )
    out = []
    for seg in segments:
        out.append(seg.text.strip())
    return "\n".join(out).strip()


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    feed_url = fetch_feed_url(PODCAST_ID)
    print(f"RSS feed: {feed_url}")

    feed = feedparser.parse(feed_url)
    episodes = list(feed.entries)

    if not episodes:
        raise RuntimeError("No episodes found in feed.")

    # RSS is usually newest-first; we’ll preserve that order
    # For fallback numbering, we’ll assign 1..N in reverse so older episodes get lower numbers
    # (if you prefer newest=1, change the indexing below).
    episodes_oldest_first = list(reversed(episodes))

    for idx, entry in enumerate(episodes_oldest_first, start=1):
        ep_num = safe_episode_number(entry, fallback_index=idx)
        ep_num_str = f"{ep_num:03d}"

        title = entry.get("title", "").strip() or f"Episode {ep_num_str}"
        base_name = f"{ep_num_str}.txt"
        out_path = OUT_DIR / base_name

        if out_path.exists() and out_path.stat().st_size > 0:
            print(f"[skip] {base_name} already exists")
            continue

        print(f"\n=== Episode {ep_num_str}: {title} ===")

        # 1) Try to fetch published transcript URL(s)
        transcript_urls = find_transcript_urls(entry)
        transcript_text = None
        for tu in transcript_urls:
            transcript_text = fetch_transcript_text(tu)
            if transcript_text and transcript_text.strip():
                print(f"[ok] downloaded transcript from: {tu}")
                break

        # 2) If no transcript, download audio + transcribe
        if not transcript_text:
            audio_url = get_audio_url(entry)
            if not audio_url:
                print("[warn] no audio enclosure found; writing placeholder")
                out_path.write_text(f"{title}\n\n(No audio URL found in RSS.)", encoding="utf-8")
                continue

            # Guess extension
            ext = os.path.splitext(audio_url.split("?")[0])[1].lower()
            if ext not in [".mp3", ".m4a", ".wav", ".ogg", ".aac"]:
                ext = ".mp3"

            audio_path = AUDIO_DIR / f"{ep_num_str}{ext}"

            if not audio_path.exists():
                download_file(audio_url, audio_path)

            print("[run] transcribing with faster-whisper...")
            transcript_text = transcribe_with_faster_whisper(audio_path)

        # 3) Save transcript
        header = f"{title}\nEpisode: {ep_num_str}\n"
        published = entry.get("published")
        if published:
            header += f"Published: {published}\n"
        header += "\n" + ("-" * 60) + "\n\n"

        out_path.write_text(header + (transcript_text or ""), encoding="utf-8")
        print(f"[saved] {out_path}")

        time.sleep(SLEEP_BETWEEN_EPISODES_SEC)

    # Cleanup audio temp if you want
    # shutil.rmtree(AUDIO_DIR, ignore_errors=True)
    print("\nDone.")


if __name__ == "__main__":
    main()
