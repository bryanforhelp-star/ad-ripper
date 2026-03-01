"""
Media downloader for AdVault Local.

Strategy (no headless browsers):
1. Fetch the snapshot HTML page.
2. Parse the HTML looking for <video>, <source>, og:video, or JSON-LD
   media URLs.
3. If a direct media URL is found, download it.
4. If nothing is found, record status=snapshot_only so the user can visit
   the snapshot URL manually.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings

logger = logging.getLogger(__name__)

# Ordered list of patterns to try when hunting for media in the snapshot HTML
_VIDEO_PATTERNS = [
    # <video src="...">
    lambda soup: [
        tag.get("src")
        for tag in soup.find_all("video")
        if tag.get("src")
    ],
    # <source src="...">
    lambda soup: [
        tag.get("src")
        for tag in soup.find_all("source")
        if tag.get("src") and tag.get("type", "").startswith("video")
    ],
    # og:video meta
    lambda soup: [
        tag.get("content")
        for tag in soup.find_all("meta", property="og:video")
        if tag.get("content")
    ],
    # og:video:url
    lambda soup: [
        tag.get("content")
        for tag in soup.find_all("meta", property="og:video:url")
        if tag.get("content")
    ],
    # data attributes
    lambda soup: [
        tag.get("data-video-src") or tag.get("data-src")
        for tag in soup.find_all(attrs={"data-video-src": True})
    ],
]

_IMAGE_PATTERNS = [
    lambda soup: [
        tag.get("src")
        for tag in soup.find_all("img", class_=re.compile(r"(ad|creative|media)", re.I))
        if tag.get("src")
    ],
    lambda soup: [
        tag.get("content")
        for tag in soup.find_all("meta", property="og:image")
        if tag.get("content")
    ],
]

# Regex to find CDN video URLs in raw HTML/JS
_CDN_VIDEO_RE = re.compile(
    r'https://[^\s\'"<>]+\.mp4[^\s\'"<>]*', re.IGNORECASE
)
_CDN_IMAGE_RE = re.compile(
    r'https://[^\s\'"<>]+\.(jpg|jpeg|png|webp)[^\s\'"<>]*', re.IGNORECASE
)


def _make_client() -> httpx.Client:
    settings = get_settings()
    return httpx.Client(
        timeout=settings.download_timeout_seconds,
        follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    )


def _retry_get(client: httpx.Client, url: str, max_retries: int = 3) -> httpx.Response:
    settings = get_settings()
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            if attempt == max_retries:
                raise
            wait = 2 ** attempt
            logger.warning(
                "Download attempt %d/%d failed (%s); retrying in %ds",
                attempt,
                max_retries,
                exc,
                wait,
            )
            time.sleep(wait)
    raise RuntimeError("unreachable")  # pragma: no cover


def _extract_media_urls(snapshot_url: str, html: str) -> dict[str, list[str]]:
    """Return {'video': [...], 'image': [...]} of candidate media URLs."""
    soup = BeautifulSoup(html, "html.parser")
    base = snapshot_url

    videos: list[str] = []
    images: list[str] = []

    for pattern_fn in _VIDEO_PATTERNS:
        found = [u for u in (pattern_fn(soup) or []) if u]
        videos.extend(found)

    for pattern_fn in _IMAGE_PATTERNS:
        found = [u for u in (pattern_fn(soup) or []) if u]
        images.extend(found)

    # Also scan raw text for CDN URLs
    videos.extend(_CDN_VIDEO_RE.findall(html))
    images.extend(_CDN_IMAGE_RE.findall(html))

    # Make absolute
    def make_absolute(url: str) -> str:
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("http"):
            return url
        return urljoin(base, url)

    return {
        "video": list(dict.fromkeys(make_absolute(u) for u in videos if u)),
        "image": list(dict.fromkeys(make_absolute(u) for u in images if u)),
    }


def _download_file(client: httpx.Client, url: str, dest: Path) -> None:
    """Stream-download *url* to *dest*."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                fh.write(chunk)
    logger.info("Downloaded %s -> %s", url, dest)


def download_ad(
    brand: str,
    meta_ad_id: str,
    snapshot_url: str,
    metadata: dict,
) -> dict:
    """
    Download media for a single ad.

    Returns a result dict with keys:
      status: 'downloaded' | 'snapshot_only' | 'failed'
      media_path: str or None
      error: str or None
    """
    settings = get_settings()
    ad_dir = Path(settings.data_dir) / _safe_name(brand) / meta_ad_id
    ad_dir.mkdir(parents=True, exist_ok=True)

    # Always save metadata
    meta_file = ad_dir / "metadata.json"
    with meta_file.open("w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2, default=str)
    logger.info("Saved metadata: %s", meta_file)

    if not snapshot_url:
        return {
            "status": "snapshot_only",
            "media_path": None,
            "error": "No snapshot_url available",
        }

    try:
        with _make_client() as client:
            logger.info("Fetching snapshot page: %s", snapshot_url)
            try:
                resp = _retry_get(client, snapshot_url)
                html = resp.text
            except Exception as exc:
                logger.warning("Could not fetch snapshot HTML: %s", exc)
                return {
                    "status": "snapshot_only",
                    "media_path": None,
                    "error": f"Could not fetch snapshot: {exc}",
                }

            media_urls = _extract_media_urls(snapshot_url, html)
            logger.debug("Found media candidates: %s", media_urls)

            # Try video first, then image
            for media_type, urls in [
                ("video", media_urls["video"]),
                ("image", media_urls["image"]),
            ]:
                for url in urls:
                    try:
                        ext = _guess_extension(url, media_type)
                        dest = ad_dir / f"media{ext}"
                        _download_file(client, url, dest)
                        return {
                            "status": "downloaded",
                            "media_path": str(dest),
                            "error": None,
                        }
                    except Exception as exc:
                        logger.warning(
                            "Failed to download %s from %s: %s", media_type, url, exc
                        )

    except Exception as exc:
        logger.error("Unexpected error downloading ad %s: %s", meta_ad_id, exc)
        return {"status": "failed", "media_path": None, "error": str(exc)}

    # Nothing downloaded
    return {
        "status": "snapshot_only",
        "media_path": None,
        "error": (
            "No direct media URL found in snapshot. "
            "Visit the snapshot_url manually to view the ad."
        ),
    }


def _safe_name(name: str) -> str:
    """Return a filesystem-safe version of *name*."""
    return re.sub(r"[^\w\-]", "_", name).strip("_") or "unknown"


def _guess_extension(url: str, media_type: str) -> str:
    path = urlparse(url).path
    for ext in (".mp4", ".mov", ".webm", ".avi", ".jpg", ".jpeg", ".png", ".webp"):
        if path.lower().endswith(ext):
            return ext
    return ".mp4" if media_type == "video" else ".jpg"
