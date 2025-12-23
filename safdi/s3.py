import json, urllib.request

PODCAST_ID="1447749859"
lookup = f"https://itunes.apple.com/lookup?id={PODCAST_ID}"
feed_url = json.loads(urllib.request.urlopen(lookup).read())["results"][0]["feedUrl"]
rss = urllib.request.urlopen(feed_url).read().decode("utf-8", "ignore")

print("RSS:", feed_url)
print("Has '<podcast:transcript' ?", "<podcast:transcript" in rss)
print("Has 'transcript' anywhere ?", "transcript" in rss.lower())
