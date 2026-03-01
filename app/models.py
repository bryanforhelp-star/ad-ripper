"""SQLModel ORM models for AdVault Local."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, Relationship, SQLModel


class JobStatus(str, Enum):
    pending = "pending"
    downloading = "downloading"
    snapshot_only = "snapshot_only"
    downloaded = "downloaded"
    transcribing = "transcribing"
    done = "done"
    failed = "failed"


# ---------------------------------------------------------------------------
# Brand
# ---------------------------------------------------------------------------


class Brand(SQLModel, table=True):
    __tablename__ = "brand"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    ads: list["Ad"] = Relationship(back_populates="brand")


# ---------------------------------------------------------------------------
# Ad
# ---------------------------------------------------------------------------


class Ad(SQLModel, table=True):
    __tablename__ = "ad"

    id: Optional[int] = Field(default=None, primary_key=True)
    brand_id: int = Field(foreign_key="brand.id", index=True)
    meta_ad_id: str = Field(index=True)
    page_name: Optional[str] = None
    ad_text: Optional[str] = None
    start_time: Optional[datetime] = None
    stop_time: Optional[datetime] = None
    snapshot_url: Optional[str] = None
    publisher_platforms: Optional[str] = None  # JSON-encoded list
    created_at: datetime = Field(default_factory=datetime.utcnow)

    brand: Optional[Brand] = Relationship(back_populates="ads")
    jobs: list["DownloadJob"] = Relationship(back_populates="ad")


# ---------------------------------------------------------------------------
# DownloadJob
# ---------------------------------------------------------------------------


class DownloadJob(SQLModel, table=True):
    __tablename__ = "downloadjob"

    id: Optional[int] = Field(default=None, primary_key=True)
    ad_id: int = Field(foreign_key="ad.id", index=True)
    status: JobStatus = Field(default=JobStatus.pending)
    error: Optional[str] = None
    media_path: Optional[str] = None
    transcript_path: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    ad: Optional[Ad] = Relationship(back_populates="jobs")
