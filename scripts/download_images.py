#!/usr/bin/env python3
"""
download_images.py — Parrot Binder offline image seeder
========================================================
Downloads card images from the Pokémon TCG CDN and stores them under
../data/images/{card_id}/small.webp  (and optionally large.webp).

Run AFTER download_data.py so cards.db already exists.

Usage
-----
    # Download small images only (~30-50 KB each, ~450 MB total for all cards)
    python scripts/download_images.py

    # Also download large images (~150-250 KB each, ~2-3 GB total)
    python scripts/download_images.py --large

    # Only a specific set
    python scripts/download_images.py --sets sv1 sv2

    # Force re-download even if file already exists
    python scripts/download_images.py --force

Disk estimates (full collection ~15 000 cards as of 2025)
    small only:   ~450 MB
    small + large ~3.0 GB

The server.py will automatically serve local images when present and fall
back to the CDN URL if not.

Requirements
------------
    pip install requests tqdm
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
    from tqdm import tqdm
except ImportError:
    print("Missing dependencies.  Run:  pip install requests tqdm")
    sys.exit(1)

DB_PATH    = Path(__file__).parent.parent / "data" / "cards.db"
IMG_DIR    = Path(__file__).parent.parent / "data" / "images"
RATE_SLEEP = 0.05   # 20 req/s — CDN is generous, but be polite
CHUNK_SIZE = 65_536


def download_file(url: str, dest: Path, session: requests.Session) -> bool:
    """Download url to dest.  Returns True on success."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30, stream=True)
            if r.status_code == 404:
                return False
            r.raise_for_status()
            with dest.open("wb") as f:
                for chunk in r.iter_content(CHUNK_SIZE):
                    f.write(chunk)
            return True
        except requests.RequestException as exc:
            if attempt == 2:
                tqdm.write(f"  FAIL {url}: {exc}")
                return False
            time.sleep(3 * (attempt + 1))
    return False


def ext_from_url(url: str) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix
    return suffix if suffix else ".png"


def main() -> None:
    parser = argparse.ArgumentParser(description="Download card images")
    parser.add_argument("--large",  action="store_true",
                        help="Also download large images (~150 KB each)")
    parser.add_argument("--sets",   nargs="*", metavar="SET_ID",
                        help="Restrict to these set IDs")
    parser.add_argument("--force",  action="store_true",
                        help="Re-download even if file already exists")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}")
        print("Run download_data.py first.")
        sys.exit(1)

    con = sqlite3.connect(DB_PATH)

    query = "SELECT id, data FROM cards"
    params: list = []
    if args.sets:
        placeholders = ",".join("?" * len(args.sets))
        query += f" WHERE set_id IN ({placeholders})"
        params = args.sets

    rows = con.execute(query, params).fetchall()
    con.close()

    print(f"Found {len(rows)} cards to process")
    sizes = ["small"] + (["large"] if args.large else [])
    print(f"Downloading: {', '.join(sizes)} images")
    IMG_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": "ParrotBinder/1.0 (offline seeder)"})

    downloaded = skipped = failed = 0

    for card_id, data_json in tqdm(rows, unit="card"):
        try:
            card = json.loads(data_json)
        except json.JSONDecodeError:
            continue

        images = card.get("images", {})
        card_dir = IMG_DIR / card_id

        for size in sizes:
            url = images.get(size)
            if not url:
                continue
            ext  = ext_from_url(url)
            dest = card_dir / f"{size}{ext}"
            if dest.exists() and not args.force:
                skipped += 1
                continue
            ok = download_file(url, dest, session)
            if ok:
                downloaded += 1
            else:
                failed += 1
            time.sleep(RATE_SLEEP)

    total_mb = sum(f.stat().st_size for f in IMG_DIR.rglob("*") if f.is_file()) / 1_048_576
    print(f"\n✓  Done — downloaded {downloaded}, skipped {skipped}, failed {failed}")
    print(f"   Image directory: {IMG_DIR}  ({total_mb:.0f} MB on disk)")


if __name__ == "__main__":
    main()
