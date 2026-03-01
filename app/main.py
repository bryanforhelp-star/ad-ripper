"""
AdVault Local — FastAPI application entry point.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlmodel import Session

from app.config import get_settings
from app.db import get_session, init_db
from app.meta_client import MetaApiError
from app.services import (
    create_download_jobs,
    list_ads,
    list_jobs,
    run_transcription,
    search_and_upsert,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

settings = get_settings()
BASE_DIR = Path(__file__).parent

app = FastAPI(
    title="AdVault Local",
    description="Search, download, and transcribe Meta Ad Library ads locally.",
    version="1.0.0",
)

# Mount static files if directory exists
static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.on_event("startup")
def on_startup() -> None:
    # Ensure data directory exists
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    # Create DB tables
    init_db()
    logger.info("AdVault Local started. DB initialised.")
    if not settings.meta_access_token:
        logger.warning(
            "META_ACCESS_TOKEN is not set. Set it in your .env file before searching."
        )


# ---------------------------------------------------------------------------
# Dependency shorthand
# ---------------------------------------------------------------------------

DbSession = Annotated[Session, Depends(get_session)]


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class DownloadRequest(BaseModel):
    brand: str
    ad_ids: list[str]


class TranscribeRequest(BaseModel):
    brand: str
    ad_id: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    template = BASE_DIR / "templates" / "index.html"
    if not template.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return HTMLResponse(template.read_text(encoding="utf-8"))


@app.get("/api/search", tags=["ads"])
def api_search(
    session: DbSession,
    brand: str = Query(..., description="Brand / advertiser name to search"),
    country: str = Query("US", description="Two-letter country code, e.g. US"),
    active_only: bool = Query(True, description="Only return active ads"),
    limit: int = Query(50, ge=1, le=200, description="Max number of ads to return"),
) -> dict:
    """Search the Meta Ad Library and upsert results into the local DB."""
    try:
        ads = search_and_upsert(
            session,
            brand=brand,
            country=country,
            active_only=active_only,
            limit=limit,
        )
        return {"ok": True, "brand": brand, "count": len(ads), "ads": ads}
    except MetaApiError as exc:
        logger.error("Meta API error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected error during search")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/download", tags=["ads"])
def api_download(body: DownloadRequest, session: DbSession) -> dict:
    """Download media for selected ads."""
    if not body.ad_ids:
        raise HTTPException(status_code=400, detail="ad_ids must not be empty")
    try:
        jobs = create_download_jobs(session, brand=body.brand, meta_ad_ids=body.ad_ids)
        return {"ok": True, "jobs": jobs}
    except Exception as exc:
        logger.exception("Unexpected error during download")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/transcribe", tags=["ads"])
def api_transcribe(body: TranscribeRequest, session: DbSession) -> dict:
    """Run Whisper transcription on a downloaded ad."""
    result = run_transcription(session, brand=body.brand, meta_ad_id=body.ad_id)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/api/ads", tags=["ads"])
def api_ads(
    session: DbSession,
    brand: str = Query(..., description="Brand name"),
) -> dict:
    """List ads stored in the local DB for a brand."""
    ads = list_ads(session, brand)
    return {"ok": True, "brand": brand, "count": len(ads), "ads": ads}


@app.get("/api/jobs", tags=["ads"])
def api_jobs(
    session: DbSession,
    brand: str = Query(..., description="Brand name"),
) -> dict:
    """List download/transcription jobs for a brand."""
    jobs = list_jobs(session, brand)
    return {"ok": True, "brand": brand, "count": len(jobs), "jobs": jobs}
