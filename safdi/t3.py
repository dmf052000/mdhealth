import os, json, time, subprocess, urllib.request, xml.etree.ElementTree as ET
from pathlib import Path

PODCAST_ID = "1447749859"
OUT_DIR = Path("transcripts")
AUDIO_DIR = Path("audio")
SLEEP_SEC = 0.25

# Choose one of these models:
MODEL = "gpt-4o-mini-transcribe"  # or "gpt-4o-transcribe"
# See OpenAI speech-to-text docs for models / endpoint.   [oai_citation:2‡OpenAI Platform](https://platform.openai.com/docs/guides/speech-to-text?utm_source=chatgpt.com)

def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()

def fetch_feed_url(podcast_id: str) -> str:
    data = json.loads(http_get(f"https://itunes.apple.com/lookup?id={podcast_id}"))
    return data["results"][0]["feedUrl"]

def get_text(el, default=""):
    return (el.text or default).strip() if el is not None else default

def find_episode_number(item, fallback: int) -> int:
    # itunes:episode
    ep = item.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}episode")
    if ep is not None and (ep.text or "").strip().isdigit():
        return int(ep.text.strip())
    return fallback

def get_enclosure_url(item) -> str | None:
    enc = item.find("enclosure")
    if enc is not None:
        return enc.attrib.get("url")
    # sometimes enclosure is namespaced
    for e in item.findall(".//{*}enclosure"):
        url = e.attrib.get("url")
        if url:
            return url
    return None

def download(url: str, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return
    data = http_get(url)
    dest.write_bytes(data)

def transcribe_with_curl(audio_path: Path) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY env var")

    # Use curl multipart upload.  [oai_citation:3‡OpenAI Developer Community](https://community.openai.com/t/request-to-gpt-4o-mini-transcribe-model/1151090/4?utm_source=chatgpt.com)
    cmd = [
        "curl", "-sS", "-L", "https://api.openai.com/v1/audio/transcriptions",
        "-H", f"Authorization: Bearer {api_key}",
        "-H", "Content-Type: multipart/form-data",
        "-F", f"file=@{str(audio_path)}",
        "-F", f"model={MODEL}",
        "-F", "response_format=json",
        "-F", "temperature=0"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed: {result.stderr.strip()}")

    j = json.loads(result.stdout)
    return (j.get("text") or "").strip()

def main():
    OUT_DIR.mkdir(exist_ok=True)
    AUDIO_DIR.mkdir(exist_ok=True)

    feed_url = fetch_feed_url(PODCAST_ID)
    print("RSS:", feed_url)

    root = ET.fromstring(http_get(feed_url))
    items = root.findall(".//item")
    if not items:
        raise RuntimeError("No episodes found in RSS")

    # Oldest -> newest so fallback numbering is stable
    items.reverse()

    for idx, item in enumerate(items, start=1):
        title = get_text(item.find("title"), f"Episode {idx}")
        ep_num = find_episode_number(item, idx)
        ep_str = f"{ep_num:03d}"

        out_txt = OUT_DIR / f"{ep_str}.txt"
        if out_txt.exists() and out_txt.stat().st_size > 0:
            print(f"[skip] {out_txt.name}")
            continue

        audio_url = get_enclosure_url(item)
        if not audio_url:
            print(f"[warn] no enclosure url for {ep_str}: {title}")
            out_txt.write_text(f"{title}\nEpisode: {ep_str}\n\n(No audio URL found in RSS.)\n", encoding="utf-8")
            continue

        # guess extension
        ext = ".mp3"
        for e in (".mp3", ".m4a", ".wav", ".aac", ".ogg"):
            if audio_url.split("?")[0].lower().endswith(e):
                ext = e
                break

        audio_path = AUDIO_DIR / f"{ep_str}{ext}"
        print(f"\nDownloading {ep_str}: {title}")
        download(audio_url, audio_path)

        print(f"Transcribing {ep_str} with {MODEL}...")
        text = transcribe_with_curl(audio_path)

        out_txt.write_text(f"{title}\nEpisode: {ep_str}\n\n" + ("-"*60) + "\n\n" + text + "\n", encoding="utf-8")
        print(f"[saved] {out_txt}")

        time.sleep(SLEEP_SEC)

    print("\nDone.")

if __name__ == "__main__":
    main()
