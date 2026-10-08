from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FileStatus(StrEnum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class MeasurementStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NOT_REQUIRED = "NOT_REQUIRED"  # Point / MultiPoint geometries
    EMPTY = "EMPTY"  # Null or empty geometry
    UNSUPPORTED = "UNSUPPORTED"  # GeometryCollection or unknown type
    INVALID_GEOMETRY = "INVALID_GEOMETRY"  # Geometry irreparable by make_valid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_hex_id() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class File(Base):
    """Represents an uploaded geospatial file (shapefile ZIP or KML)."""

    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_hex_id)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)  # "shapefile" | "kml"
    feature_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crs: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=FileStatus.PROCESSING)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    features: Mapped[list[Feature]] = relationship(
        "Feature", back_populates="file", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<File id={self.id!r} filename={self.filename!r} status={self.status!r}>"


class Feature(Base):
    """Represents a single geospatial feature extracted from a File."""

    __tablename__ = "features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("files.id", ondelete="CASCADE"), nullable=False, index=True
    )
    feature_index: Mapped[int] = mapped_column(Integer, nullable=False)
    geometry_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Stored as GeoJSON text for portability; no PostGIS required
    geometry: Mapped[str | None] = mapped_column(Text, nullable=True)
    crs: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Arbitrary feature properties stored as JSON text
    properties: Mapped[str | None] = mapped_column(Text, nullable=True)
    measurement_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MeasurementStatus.PENDING
    )
    # Computed measurements stored as JSON text, e.g. {"area_m2": 1234.5, "length_m": null}
    measurements: Mapped[str | None] = mapped_column(Text, nullable=True)
    projected_crs: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Non-fatal warnings produced during processing, stored as JSON array text
    warnings: Mapped[str | None] = mapped_column(Text, nullable=True)

    file: Mapped[File] = relationship("File", back_populates="features")

    def __repr__(self) -> str:
        return (
            f"<Feature id={self.id!r} file_id={self.file_id!r}"
            f" index={self.feature_index!r} type={self.geometry_type!r}>"
        )
