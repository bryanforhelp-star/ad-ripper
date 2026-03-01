"""Client for the Meta Ad Library API (ads_archive endpoint)."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# Fields we request from the API
AD_FIELDS = ",".join(
    [
        "id",
        "page_name",
        "ad_snapshot_url",
        "ad_delivery_start_time",
        "ad_delivery_stop_time",
        "ad_creative_bodies",
        "publisher_platforms",
    ]
)


class MetaApiError(Exception):
    """Raised when the Meta API returns an error response."""

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


def _build_client() -> httpx.Client:
    settings = get_settings()
    return httpx.Client(
        timeout=30.0,
        headers={"User-Agent": "AdVaultLocal/1.0"},
        base_url=f"{settings.meta_api_base_url}/{settings.meta_ads_api_version}",
    )


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S+0000", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            # Convert to naive UTC
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except ValueError:
            continue
    logger.warning("Could not parse datetime: %s", value)
    return None


def search_ads(
    brand: str,
    country: str = "US",
    active_only: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    Search the Meta Ad Library for ads matching *brand*.

    Returns a list of normalised ad dicts ready for upserting into the DB.
    Raises MetaApiError on API-level failures.
    """
    settings = get_settings()

    if not settings.meta_access_token:
        raise MetaApiError(
            "META_ACCESS_TOKEN is not set. Add it to your .env file and restart."
        )

    params: dict[str, Any] = {
        "search_terms": brand,
        "ad_reached_countries": country,
        "ad_active_status": "ACTIVE" if active_only else "ALL",
        "fields": AD_FIELDS,
        "limit": min(limit, 100),
        "access_token": settings.meta_access_token,
    }

    ads: list[dict[str, Any]] = []
    retries = 3

    with _build_client() as client:
        url = "/ads_archive"
        attempt = 0
        while url and len(ads) < limit:
            attempt += 1
            try:
                logger.info("Meta API request: %s (page %d)", url, attempt)
                resp = client.get(url, params=params if attempt == 1 else None)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                _handle_http_error(exc, retries)
                break
            except httpx.RequestError as exc:
                logger.error("Network error calling Meta API: %s", exc)
                if attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                raise MetaApiError(f"Network error: {exc}") from exc

            payload = resp.json()

            if "error" in payload:
                err = payload["error"]
                raise MetaApiError(
                    f"Meta API error: {err.get('message', err)}",
                    code=err.get("code"),
                )

            for raw in payload.get("data", []):
                ads.append(_normalise(raw))

            # Pagination
            paging = payload.get("paging", {})
            next_url = paging.get("next")
            if next_url and len(ads) < limit:
                # next_url is a full URL; switch to absolute request
                url = next_url
                params = {}  # params are baked into next_url
            else:
                url = None

    logger.info("Fetched %d ads for brand=%r country=%s", len(ads), brand, country)
    return ads[:limit]


def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw API ad record to a clean internal dict."""
    bodies = raw.get("ad_creative_bodies") or []
    ad_text = " | ".join(bodies) if bodies else None

    platforms = raw.get("publisher_platforms") or []

    return {
        "meta_ad_id": str(raw["id"]),
        "page_name": raw.get("page_name"),
        "ad_text": ad_text,
        "start_time": _parse_datetime(raw.get("ad_delivery_start_time")),
        "stop_time": _parse_datetime(raw.get("ad_delivery_stop_time")),
        "snapshot_url": raw.get("ad_snapshot_url"),
        "publisher_platforms": platforms,
    }


def _handle_http_error(exc: httpx.HTTPStatusError, max_retries: int) -> None:
    status = exc.response.status_code
    try:
        body = exc.response.json()
        msg = body.get("error", {}).get("message", str(exc))
        code = body.get("error", {}).get("code")
    except Exception:
        msg = str(exc)
        code = None

    if status == 401:
        raise MetaApiError(
            "Authentication failed. Check your META_ACCESS_TOKEN.", code=code
        ) from exc
    if status == 403:
        raise MetaApiError(
            "Access denied by Meta API. Verify token permissions.", code=code
        ) from exc
    if status == 429:
        logger.warning("Meta API rate limit hit; backing off…")
        time.sleep(60)
        return  # caller will retry
    raise MetaApiError(f"Meta API HTTP {status}: {msg}", code=code) from exc
