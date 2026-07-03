import yaml
import csv
import os
from tqdm import tqdm
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import time

session = requests.Session()
retries = Retry(
    total=5,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
)
session.mount("https://", HTTPAdapter(max_retries=retries))
session.mount("http://", HTTPAdapter(max_retries=retries))


def get_with_retry(url, params):
    for attempt in range(5):
        try:
            return session.get(url, params=params, timeout=30).json()
        except requests.exceptions.RequestException as e:
            if attempt == 4:
                raise
            wait = 2 ** attempt
            print(f"\nNetwork error ({e}), retrying in {wait}s...")
            time.sleep(wait)


def process_video(video_snippet):
    temp_dict = {}
    temp_dict["video_id"] = video_snippet["resourceId"]["videoId"]
    temp_dict["title"] = video_snippet["title"]
    temp_dict["video_published_at"] = video_snippet["publishedAt"]
    return temp_dict


with open("config.yml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

API_KEY = config["API_KEY"]
CHANNELS_API_URL = "https://www.googleapis.com/youtube/v3/channels"
PLAYLIST_API_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
VIDEOS_API_URL = "https://www.googleapis.com/youtube/v3/videos"
OUTPUT_FOLDER = config["output_folder"]
OUTPUT_FIELDS = ["video_id", "title", "video_published_at", "view_count"]
channel_ids = config["channel_ids"]

channels_params = {
    "key": API_KEY,
    "part": "contentDetails,snippet",
}


class QuotaExceeded(Exception):
    pass


def check_quota(r):
    if r.get("error", {}).get("errors", [{}])[0].get("reason") in (
        "quotaExceeded",
        "dailyLimitExceeded",
    ):
        raise QuotaExceeded()

playlist_params = {
    "key": API_KEY,
    "part": "snippet",
    "maxResults": 50,
}

videos_params = {
    "key": API_KEY,
    "part": "statistics",
}


def get_view_counts(video_ids):
    view_counts = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        videos_params.update({"id": ",".join(batch)})
        r = get_with_retry(VIDEOS_API_URL, videos_params)
        check_quota(r)
        for item in r.get("items", []):
            view_counts[item["id"]] = item["statistics"].get("viewCount", "")
        time.sleep(0.1)
    return view_counts

seen_titles = set()

try:
    for channel_id in channel_ids:
        channels_params.update({"id": channel_id})

        r = get_with_retry(CHANNELS_API_URL, channels_params)
        check_quota(r)

        if not r.get("items"):
            print(f"Skipping {channel_id}: channel not found")
            continue

        channel_name = r["items"][0]["snippet"]["title"]
        # some channels share an identical display name; the first channel
        # with a given title (per config order) uses the plain filename,
        # any later one with the same title is forced onto a channel_id
        # suffixed filename so they don't collide with each other
        legacy_path = os.path.join(
            OUTPUT_FOLDER, f"{channel_name}.csv".replace(os.sep, "_")
        )
        disambiguated_path = os.path.join(
            OUTPUT_FOLDER, f"{channel_name} ({channel_id}).csv".replace(os.sep, "_")
        )
        if channel_name in seen_titles:
            output_path = disambiguated_path
        else:
            seen_titles.add(channel_name)
            output_path = legacy_path

        if os.path.exists(output_path):
            print(f"Skipping {channel_name} ({channel_id}): already scraped")
            continue

        # the uploads_id indicates the playlist where a channel's uploads are located
        uploads_id = r["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

        playlist_params.update({"playlistId": uploads_id})
        r = get_with_retry(PLAYLIST_API_URL, playlist_params)
        check_quota(r)

        if "items" in r:
            pageToken = r.get("nextPageToken")
            print(f"Scraping {channel_name}'s videos:")
            pbar = tqdm(total=r["pageInfo"]["totalResults"])

            videos = [process_video(video["snippet"]) for video in r["items"]]
            pbar.update(len(r["items"]))

            # process the rest
            while pageToken:
                playlist_params.update({"pageToken": pageToken})
                r = get_with_retry(PLAYLIST_API_URL, playlist_params)
                check_quota(r)
                videos.extend(process_video(video["snippet"]) for video in r["items"])
                pbar.update(len(r["items"]))
                pageToken = r.get("nextPageToken")
                time.sleep(0.1)
            pbar.close()
            # reset pageToken for new channel
            playlist_params.update({"pageToken": None})

            view_counts = get_view_counts([video["video_id"] for video in videos])
            for video in videos:
                video["view_count"] = view_counts.get(video["video_id"], "")

            with open(output_path, "w", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
                w.writeheader()
                w.writerows(videos)
except QuotaExceeded:
    print("\nYouTube API daily quota exceeded. Stopping cleanly — already-scraped "
          "channels are saved. Re-run this script after the quota resets to pick up "
          "where it left off (already-completed channels are skipped automatically).")
