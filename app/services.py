"""
Business logic layer: orchestrates DB operations, Meta API calls,
downloads, and transcriptions.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlmodel import Session, select

from app.config import get_settings
from app.downloader import download_ad
from app.meta_client import MetaApiError, search_ads
from app.models import Ad, Brand, DownloadJob, JobStatus
from app.transcriber import TranscribeError, transcribe_media

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.utcnow()


def _days_running(start: Optional[datetime], stop: Optional[datetime]) -> Optional[int]:
    if start is None:
        return None
    end = stop if stop else _utcnow()
    return max(0, (end - start).days)


def _ad_to_dict(ad: Ad) -> dict[str, Any]:
    platforms: list[str] = []
    if ad.publisher_platforms:
        try:
            platforms = json.loads(ad.publisher_platforms)
        except (json.JSONDecodeError, TypeError):
            platforms = [ad.publisher_platforms]

    return {
        "id": ad.id,
        "meta_ad_id": ad.meta_ad_id,
        "page_name": ad.page_name,
        "ad_text": ad.ad_text,
        "start_time": ad.start_time.isoformat() if ad.start_time else None,
        "stop_time": ad.stop_time.isoformat() if ad.stop_time else None,
        "days_running": _days_running(ad.start_time, ad.stop_time),
        "snapshot_url": ad.snapshot_url,
        "platforms": platforms,
    }


def _job_to_dict(job: DownloadJob, ad: Optional[Ad] = None) -> dict[str, Any]:
    return {
        "id": job.id,
        "ad_id": job.ad_id,
        "meta_ad_id": ad.meta_ad_id if ad else None,
        "status": job.status,
        "error": job.error,
        "media_path": job.media_path,
        "transcript_path": job.transcript_path,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Brand
# ---------------------------------------------------------------------------


def get_or_create_brand(session: Session, name: str) -> Brand:
    brand = session.exec(
        select(Brand).where(Brand.name == name)
    ).first()
    if brand is None:
        brand = Brand(name=name)
        session.add(brand)
        session.commit()
        session.refresh(brand)
        logger.info("Created brand: %s", name)
    return brand


# ---------------------------------------------------------------------------
# Search / upsert
# ---------------------------------------------------------------------------


def search_and_upsert(
    session: Session,
    brand: str,
    country: str = "US",
    active_only: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    1. Call Meta API.
    2. Upsert results into DB.
    3. Return list of ad dicts.
    """
    raw_ads = search_ads(brand, country=country, active_only=active_only, limit=limit)

    brand_obj = get_or_create_brand(session, brand)
    results: list[dict[str, Any]] = []

    for raw in raw_ads:
        meta_ad_id = raw["meta_ad_id"]
        existing = session.exec(
            select(Ad).where(Ad.meta_ad_id == meta_ad_id)
        ).first()

        platforms_json = json.dumps(raw.get("publisher_platforms", []))

        if existing:
            # Update mutable fields
            existing.page_name = raw.get("page_name", existing.page_name)
            existing.ad_text = raw.get("ad_text", existing.ad_text)
            existing.start_time = raw.get("start_time", existing.start_time)
            existing.stop_time = raw.get("stop_time", existing.stop_time)
            existing.snapshot_url = raw.get("snapshot_url", existing.snapshot_url)
            existing.publisher_platforms = platforms_json
            session.add(existing)
            ad = existing
        else:
            ad = Ad(
                brand_id=brand_obj.id,
                meta_ad_id=meta_ad_id,
                page_name=raw.get("page_name"),
                ad_text=raw.get("ad_text"),
                start_time=raw.get("start_time"),
                stop_time=raw.get("stop_time"),
                snapshot_url=raw.get("snapshot_url"),
                publisher_platforms=platforms_json,
            )
            session.add(ad)

    session.commit()

    # Re-fetch to return IDs
    brand_obj_id = brand_obj.id
    ads = session.exec(
        select(Ad)
        .where(Ad.brand_id == brand_obj_id)
        .order_by(Ad.start_time.desc())  # type: ignore[arg-type]
        .limit(limit)
    ).all()

    return [_ad_to_dict(a) for a in ads]


# ---------------------------------------------------------------------------
# List ads / jobs
# ---------------------------------------------------------------------------


def list_ads(session: Session, brand: str) -> list[dict[str, Any]]:
    brand_obj = session.exec(select(Brand).where(Brand.name == brand)).first()
    if brand_obj is None:
        return []
    ads = session.exec(
        select(Ad)
        .where(Ad.brand_id == brand_obj.id)
        .order_by(Ad.start_time.desc())  # type: ignore[arg-type]
    ).all()
    return [_ad_to_dict(a) for a in ads]


def list_jobs(session: Session, brand: str) -> list[dict[str, Any]]:
    brand_obj = session.exec(select(Brand).where(Brand.name == brand)).first()
    if brand_obj is None:
        return []
    ads = session.exec(select(Ad).where(Ad.brand_id == brand_obj.id)).all()
    ad_map = {a.id: a for a in ads}
    ad_ids = [a.id for a in ads]
    if not ad_ids:
        return []
    jobs = session.exec(
        select(DownloadJob).where(DownloadJob.ad_id.in_(ad_ids))  # type: ignore[attr-defined]
    ).all()
    return [_job_to_dict(j, ad_map.get(j.ad_id)) for j in jobs]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def create_download_jobs(
    session: Session,
    brand: str,
    meta_ad_ids: list[str],
) -> list[dict[str, Any]]:
    """
    For each ad_id, create (or reuse pending) DownloadJob and run download.
    Returns list of job dicts.
    """
    brand_obj = get_or_create_brand(session, brand)
    results: list[dict[str, Any]] = []

    for meta_ad_id in meta_ad_ids:
        ad = session.exec(
            select(Ad).where(Ad.meta_ad_id == meta_ad_id)
        ).first()
        if ad is None:
            logger.warning("Ad %s not found in DB; skipping.", meta_ad_id)
            results.append(
                {
                    "meta_ad_id": meta_ad_id,
                    "status": "failed",
                    "error": f"Ad {meta_ad_id} not found. Run search first.",
                }
            )
            continue

        # Find or create job
        job = session.exec(
            select(DownloadJob)
            .where(DownloadJob.ad_id == ad.id)
            .order_by(DownloadJob.created_at.desc())  # type: ignore[arg-type]
        ).first()

        if job is None or job.status == JobStatus.failed:
            job = DownloadJob(ad_id=ad.id, status=JobStatus.pending)
            session.add(job)
            session.commit()
            session.refresh(job)

        # Update to downloading
        job.status = JobStatus.downloading
        job.updated_at = _utcnow()
        session.add(job)
        session.commit()

        # Build metadata dict for saving
        metadata = {
            "meta_ad_id": ad.meta_ad_id,
            "page_name": ad.page_name,
            "ad_text": ad.ad_text,
            "start_time": ad.start_time.isoformat() if ad.start_time else None,
            "stop_time": ad.stop_time.isoformat() if ad.stop_time else None,
            "snapshot_url": ad.snapshot_url,
            "publisher_platforms": json.loads(ad.publisher_platforms or "[]"),
        }

        try:
            result = download_ad(
                brand=brand,
                meta_ad_id=meta_ad_id,
                snapshot_url=ad.snapshot_url or "",
                metadata=metadata,
            )
            job.status = JobStatus(result["status"])
            job.media_path = result.get("media_path")
            job.error = result.get("error")
        except Exception as exc:
            logger.exception("Download failed for ad %s", meta_ad_id)
            job.status = JobStatus.failed
            job.error = str(exc)

        job.updated_at = _utcnow()
        session.add(job)
        session.commit()
        session.refresh(job)
        results.append(_job_to_dict(job, ad))

    return results


# ---------------------------------------------------------------------------
# Transcribe
# ---------------------------------------------------------------------------


def run_transcription(
    session: Session,
    brand: str,
    meta_ad_id: str,
) -> dict[str, Any]:
    """
    Find the latest completed DownloadJob for *meta_ad_id* and run Whisper.
    Returns a result dict.
    """
    ad = session.exec(
        select(Ad).where(Ad.meta_ad_id == meta_ad_id)
    ).first()
    if ad is None:
        return {"ok": False, "error": f"Ad {meta_ad_id} not found. Run search first."}

    job = session.exec(
        select(DownloadJob)
        .where(DownloadJob.ad_id == ad.id)
        .order_by(DownloadJob.created_at.desc())  # type: ignore[arg-type]
    ).first()

    if job is None:
        return {"ok": False, "error": "No download job found. Download the ad first."}

    if job.status not in (JobStatus.downloaded, JobStatus.done):
        return {
            "ok": False,
            "error": (
                f"Ad is not downloaded (status={job.status}). "
                "Download must succeed before transcription."
            ),
        }

    if not job.media_path or not Path(job.media_path).exists():
        return {
            "ok": False,
            "error": "Media file not found on disk. Re-download the ad.",
        }

    settings = get_settings()
    ad_dir = Path(settings.data_dir) / _safe_name(brand) / meta_ad_id

    job.status = JobStatus.transcribing
    job.updated_at = _utcnow()
    session.add(job)
    session.commit()

    try:
        result = transcribe_media(Path(job.media_path), ad_dir)
        job.status = JobStatus.done
        job.transcript_path = result["transcript_path"]
        job.error = None
        job.updated_at = _utcnow()
        session.add(job)
        session.commit()
        session.refresh(job)
        return {
            "ok": True,
            "transcript": result["text"],
            "transcript_path": result["transcript_path"],
            "srt_path": result.get("srt_path"),
            "job": _job_to_dict(job, ad),
        }
    except TranscribeError as exc:
        job.status = JobStatus.failed
        job.error = str(exc)
        job.updated_at = _utcnow()
        session.add(job)
        session.commit()
        return {"ok": False, "error": str(exc)}


def _safe_name(name: str) -> str:
    import re
    return re.sub(r"[^\w\-]", "_", name).strip("_") or "unknown"
