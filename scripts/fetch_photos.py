#!/usr/bin/env python3
import calendar
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from io import BytesIO

import feedparser
import requests
from PIL import Image

RSS_URL = os.environ.get("GLASS_RSS_URL", "").strip()
LIMIT = int(os.environ.get("FEED_LIMIT", "60"))
THUMB_SIZE = int(os.environ.get("THUMB_SIZE", "640"))
PREVIEW_MAX_SIZE = int(os.environ.get("PREVIEW_MAX_SIZE", "900"))

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA_DIR = os.path.join(ROOT, "data")
OUT_PATH = os.path.join(DATA_DIR, "photos.json")
THUMB_DIR = os.path.join(ROOT, "static", "thumbs", "photos")
PREVIEW_DIR = os.path.join(ROOT, "static", "thumbs", "photos_preview")
THUMB_URL_PREFIX = "/static/thumbs/photos"
PREVIEW_URL_PREFIX = "/static/thumbs/photos_preview"

IMG_TAG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
}


def fetch_bytes(url, timeout=20):
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def slug_for(entry, image_url):
    base = entry.get("id") or entry.get("link") or image_url
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def entry_timestamp(entry):
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            try:
                return calendar.timegm(val)
            except (TypeError, OverflowError):
                continue
    return 0


def compute_pinned_flags(entries_with_images):
    timestamps = [entry_timestamp(entry) for entry, _ in entries_with_images]
    n = len(timestamps)
    if n == 0:
        return []

    pinned = [False] * n
    split = n - 1
    for i in range(n - 2, -1, -1):
        if timestamps[i] >= timestamps[i + 1]:
            split = i
        else:
            break

    for i in range(split):
        pinned[i] = True
    return pinned


def make_square_thumbnail(img, out_path, size=THUMB_SIZE):
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    cropped = img.crop((left, top, left + side, top + side))
    cropped = cropped.resize((size, size), Image.LANCZOS)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cropped.save(out_path, "WEBP", quality=82, method=6)


def make_preview(img, out_path, max_size=PREVIEW_MAX_SIZE):
    w, h = img.size
    scale = min(max_size / w, max_size / h, 1)
    new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
    preview = img.resize(new_size, Image.LANCZOS)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    preview.save(out_path, "WEBP", quality=85, method=6)


def ensure_thumbnails(entry, image_url):
    slug = slug_for(entry, image_url)
    thumb_web = f"{THUMB_URL_PREFIX}/{slug}.webp"
    preview_web = f"{PREVIEW_URL_PREFIX}/{slug}.webp"
    thumb_abs = os.path.join(THUMB_DIR, f"{slug}.webp")
    preview_abs = os.path.join(PREVIEW_DIR, f"{slug}.webp")

    if os.path.exists(thumb_abs) and os.path.exists(preview_abs):
        orientation = "portrait"
        try:
            with Image.open(preview_abs) as p:
                orientation = "portrait" if p.height >= p.width else "landscape"
        except Exception:
            pass
        return thumb_web, preview_web, orientation, slug

    try:
        raw_image = fetch_bytes(image_url, timeout=30)
        img = Image.open(BytesIO(raw_image)).convert("RGB")
        orientation = "portrait" if img.height >= img.width else "landscape"
        make_square_thumbnail(img, thumb_abs)
        make_preview(img, preview_abs)
        return thumb_web, preview_web, orientation, slug
    except Exception as exc:
        print(f"::warning::Thumbnail für {image_url} fehlgeschlagen ({exc}), nutze Original-URL", file=sys.stderr)
        return image_url, image_url, "landscape", None


def cleanup_orphaned(keep_slugs, directory):
    if not os.path.isdir(directory):
        return
    for fname in os.listdir(directory):
        if fname.startswith("."):
            continue
        slug = fname.rsplit(".", 1)[0]
        if slug not in keep_slugs:
            try:
                os.remove(os.path.join(directory, fname))
            except OSError:
                pass


def extract_image(entry):
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key)
        if media:
            url = media[0].get("url")
            if url:
                return url

    for enc in entry.get("enclosures", []):
        etype = enc.get("type", "")
        if etype.startswith("image/") and enc.get("href"):
            return enc["href"]

    html_blobs = []
    if entry.get("content"):
        html_blobs.extend(c.get("value", "") for c in entry["content"])
    if entry.get("summary"):
        html_blobs.append(entry["summary"])

    for blob in html_blobs:
        match = IMG_TAG_RE.search(blob)
        if match:
            return match.group(1)

    return None


def main():
    if not RSS_URL:
        print("::error::GLASS_RSS_URL ist nicht gesetzt.", file=sys.stderr)
        sys.exit(1)

    try:
        raw = fetch_bytes(RSS_URL)
    except requests.RequestException as exc:
        print(f"::error::Feed konnte nicht abgerufen werden: {exc}", file=sys.stderr)
        sys.exit(1)

    parsed = feedparser.parse(raw)

    if parsed.bozo and not parsed.entries:
        snippet = raw[:300].decode("utf-8", errors="replace")
        print(f"::error::Feed konnte nicht gelesen werden: {parsed.bozo_exception}", file=sys.stderr)
        print(f"::error::Antwort-Ausschnitt: {snippet}", file=sys.stderr)
        sys.exit(1)

    # Original-Feed-Reihenfolge beibehalten (Glass zeigt gepinnte Fotos oben,
    # unabhängig vom Datum) - hier wird NICHT nach Datum sortiert.
    entries_with_images = []
    for entry in parsed.entries:
        image = extract_image(entry)
        if image:
            entries_with_images.append((entry, image))

    pinned_flags = compute_pinned_flags(entries_with_images)

    selected = entries_with_images[:LIMIT]
    selected_pinned = pinned_flags[:LIMIT]

    featured_key = None
    if selected:
        featured_key = max(selected, key=lambda pair: entry_timestamp(pair[0]))[0]

    photos = []
    featured_photo = None
    keep_slugs = set()

    for (entry, image), is_pinned in zip(selected, selected_pinned):
        thumb_web, preview_web, orientation, slug = ensure_thumbnails(entry, image)
        if slug:
            keep_slugs.add(slug)

        photo = {
            "image": thumb_web,
            "image_preview": preview_web,
            "image_original": image,
            "orientation": orientation,
            "link": entry.get("link", RSS_URL),
            "title": entry.get("title", ""),
            "published": entry.get("published", ""),
            "pinned": is_pinned,
        }
        photos.append(photo)

        if entry is featured_key:
            featured_photo = photo

    cleanup_orphaned(keep_slugs, THUMB_DIR)
    cleanup_orphaned(keep_slugs, PREVIEW_DIR)

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": RSS_URL,
        "featured": featured_photo,
        "photos": photos,
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"{len(photos)} Fotos geschrieben nach {OUT_PATH}")


if __name__ == "__main__":
    main()
