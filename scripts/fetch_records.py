#!/usr/bin/env python3
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

USERNAME = os.environ.get("DISCOGS_USERNAME", "").strip()
TOKEN = os.environ.get("DISCOGS_TOKEN", "").strip()
LIMIT = int(os.environ.get("RECORDS_LIMIT", "0")) or None

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA_DIR = os.path.join(ROOT, "data")
OUT_PATH = os.path.join(DATA_DIR, "records.json")
FALLBACK_COVER = "/static/img/vinyl-fallback.svg"

USER_AGENT = "TomLinkPageRecordsBot/1.0 +https://github.com/REPLACE_ME/REPLACE_ME"

DISAMBIG_RE = re.compile(r"\s*\(\d+\)\s*$")


def api_headers():
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if TOKEN:
        headers["Authorization"] = f"Discogs token={TOKEN}"
    return headers


def fetch_page(page):
    url = f"https://api.discogs.com/users/{USERNAME}/collection/folders/0/releases"
    params = {"page": page, "per_page": 100, "sort": "added", "sort_order": "desc"}
    resp = requests.get(url, headers=api_headers(), params=params, timeout=20)
    if resp.status_code == 404:
        raise RuntimeError(f"Discogs-User '{USERNAME}' nicht gefunden (404).")
    if resp.status_code == 403:
        raise RuntimeError(
            "Discogs hat den Zugriff verweigert (403). Wahrscheinlich ist die "
            "Sammlung privat — entweder in den Discogs-Einstellungen auf "
            "öffentlich stellen, oder DISCOGS_TOKEN als Secret setzen."
        )
    resp.raise_for_status()
    return resp.json()


def clean_artist_name(name):
    return DISAMBIG_RE.sub("", name).strip()


def resolve_cover(cover_url):
    if not cover_url or "spacer.gif" in cover_url:
        return FALLBACK_COVER, True
    return cover_url, False


def main():
    if not USERNAME:
        print("::error::DISCOGS_USERNAME ist nicht gesetzt.", file=sys.stderr)
        sys.exit(1)

    records = []
    page = 1
    total_pages = 1

    while page <= total_pages:
        try:
            data = fetch_page(page)
        except (requests.RequestException, RuntimeError) as exc:
            print(f"::error::{exc}", file=sys.stderr)
            sys.exit(1)

        total_pages = data.get("pagination", {}).get("pages", 1)

        for release in data.get("releases", []):
            info = release.get("basic_information", {})
            release_id = release.get("id") or info.get("id")
            if not release_id:
                continue

            artists = info.get("artists", [])
            artist_name = ", ".join(clean_artist_name(a.get("name", "")) for a in artists) or "Unbekannt"

            cover_url = info.get("cover_image") or info.get("thumb")
            cover, is_fallback = resolve_cover(cover_url)

            records.append({
                "artist": artist_name,
                "title": info.get("title", ""),
                "year": info.get("year") or None,
                "cover": cover,
                "cover_is_fallback": is_fallback,
                "link": f"https://www.discogs.com/release/{release_id}",
                "date_added": release.get("date_added", ""),
            })

            if LIMIT and len(records) >= LIMIT:
                break

        if LIMIT and len(records) >= LIMIT:
            break

        page += 1
        if page <= total_pages:
            time.sleep(1)

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": f"https://www.discogs.com/user/{USERNAME}/collection",
        "records": records,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"{len(records)} Platten geschrieben nach {OUT_PATH}")


if __name__ == "__main__":
    main()
