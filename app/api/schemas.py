"""Pydantic v2 schemas for request/response bodies.

All schemas include OpenAPI ``examples`` metadata so the auto-generated docs
are self-documenting without any extra annotations in the routes.
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Error envelope (matches global error handler output)
# ---------------------------------------------------------------------------


class ErrorDetail(BaseModel):
    code: str = Field(examples=["VALIDATION_ERROR"])
    message: str = Field(examples=["File has a .zip extension but is not a valid ZIP archive."])


class ErrorResponse(BaseModel):
    """Standard error envelope returned on all 4xx/5xx responses."""

    error: ErrorDetail

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": {
                    "code": "NOT_FOUND",
                    "message": "File 'abc123' not found.",
                }
            }
        }
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str = Field(default="ok", examples=["ok"])
    version: str = Field(examples=["0.1.0"])


# ---------------------------------------------------------------------------
# File schemas
# ---------------------------------------------------------------------------


class FileUploadResponse(BaseModel):
    """Minimal response returned immediately after a successful upload + process."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
                "filename": "roads.zip",
                "feature_count": 142,
                "crs": "EPSG:4326",
                "status": "COMPLETED",
            }
        },
    )

    id: str = Field(examples=["a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"])
    filename: str = Field(examples=["roads.zip"])
    feature_count: int | None = Field(default=None, examples=[142])
    crs: str | None = Field(default=None, examples=["EPSG:4326"])
    status: str = Field(examples=["COMPLETED"])


class FileResponse(BaseModel):
    """Full file metadata returned by GET /files/{id}/."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
                "filename": "roads.zip",
                "file_type": "shapefile",
                "feature_count": 142,
                "crs": "EPSG:4326",
                "status": "COMPLETED",
                "error": None,
                "created_at": "2024-06-01T08:00:00Z",
            }
        },
    )

    id: str
    filename: str
    file_type: str
    feature_count: int | None
    crs: str | None
    status: str
    error: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Measurement item (one feature row in the measurements endpoint)
# ---------------------------------------------------------------------------


class MeasurementsDict(BaseModel):
    """Parsed measurement values for a feature."""

    area_m2: float | None = None
    area_ha: float | None = None
    perimeter_m: float | None = None
    length_m: float | None = None
    length_km: float | None = None


class MeasurementItem(BaseModel):
    """One feature's measurement result, returned in the measurements endpoint."""

    model_config = ConfigDict(from_attributes=True)

    feature_index: int
    geometry_type: str | None
    # geometry is returned as a parsed dict (or None) when include_geometry=True,
    # as None when include_geometry=False
    geometry: dict[str, Any] | None = Field(
        default=None,
        description="GeoJSON geometry object. Null when include_geometry=false.",
        examples=[
            {
                "type": "Polygon",
                "coordinates": [[[9.0, 51.0], [9.1, 51.0], [9.1, 51.1], [9.0, 51.0]]],
            }
        ],
    )
    crs: str | None = Field(default=None, examples=["EPSG:4326"])
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Feature attribute properties.",
        examples=[{"name": "Highway A5", "lanes": 4}],
    )
    projected_crs: str | None = Field(default=None, examples=["EPSG:32632"])
    measurement_status: str = Field(examples=["COMPLETED"])
    measurements: MeasurementsDict | None = Field(
        default=None,
        examples=[
            {
                "area_m2": 15432.7,
                "area_ha": 1.543,
                "perimeter_m": 498.1,
                "length_m": None,
                "length_km": None,
            }
        ],
    )
    warnings: list[str] = Field(
        default_factory=list,
        examples=[["Feature 3: geometry was invalid; automatically repaired with make_valid."]],
    )


# ---------------------------------------------------------------------------
# Summary block for the measurements endpoint
# ---------------------------------------------------------------------------


class MeasurementSummary(BaseModel):
    """Aggregated totals across all features of a file."""

    total_area_m2: float = Field(
        default=0.0,
        description="Sum of area_m2 for all COMPLETED polygon features.",
        examples=[987654.3],
    )
    total_length_m: float = Field(
        default=0.0,
        description="Sum of length_m for all COMPLETED line features.",
        examples=[43210.5],
    )
    counts_by_status: dict[str, int] = Field(
        default_factory=dict,
        description="Feature count grouped by measurement_status.",
        examples=[{"COMPLETED": 138, "NOT_REQUIRED": 2, "EMPTY": 2}],
    )


# ---------------------------------------------------------------------------
# Measurements endpoint response
# ---------------------------------------------------------------------------


class MeasurementsResponse(BaseModel):
    """Response for GET /files/{id}/measurements/."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "file_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
                "total": 142,
                "limit": 50,
                "offset": 0,
                "summary": {
                    "total_area_m2": 987654.3,
                    "total_length_m": 43210.5,
                    "counts_by_status": {"COMPLETED": 138, "NOT_REQUIRED": 2, "EMPTY": 2},
                },
                "items": [],
            }
        }
    )

    file_id: str
    total: int = Field(description="Total number of features for this file (unpaginated).")
    limit: int
    offset: int
    summary: MeasurementSummary
    items: list[MeasurementItem]


# ---------------------------------------------------------------------------
# Pagination wrapper (generic list endpoints)
# ---------------------------------------------------------------------------


class PaginatedResponse(BaseModel):
    total: int
    items: list[Any]


# ---------------------------------------------------------------------------
# Legacy feature response (kept for backward-compat with stub GET /features)
# ---------------------------------------------------------------------------


class FeatureResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    file_id: str
    feature_index: int
    geometry_type: str | None
    geometry: str | None = Field(description="GeoJSON geometry as a JSON string")
    crs: str | None
    properties: str | None = Field(description="Feature properties as a JSON string")
    measurement_status: str
    measurements: str | None = Field(description="Measurements as a JSON string")
    projected_crs: str | None
    warnings: str | None = Field(description="Warnings as a JSON array string")


# ---------------------------------------------------------------------------
# Assembly helpers: ORM Feature → MeasurementItem
# ---------------------------------------------------------------------------


def _parse_json_field(raw: str | None) -> Any:
    """Safely parse a JSON text column; return None on failure."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def feature_to_measurement_item(
    feature: Any,  # app.db.models.Feature ORM object
    *,
    include_geometry: bool,
) -> MeasurementItem:
    """Convert an ORM Feature row to a MeasurementItem schema."""
    raw_geometry = _parse_json_field(feature.geometry)
    raw_properties = _parse_json_field(feature.properties) or {}
    raw_measurements = _parse_json_field(feature.measurements)
    raw_warnings = _parse_json_field(feature.warnings) or []

    measurements_model: MeasurementsDict | None = None
    if raw_measurements is not None:
        measurements_model = MeasurementsDict(**raw_measurements)

    return MeasurementItem(
        feature_index=feature.feature_index,
        geometry_type=feature.geometry_type,
        geometry=raw_geometry if include_geometry else None,
        crs=feature.crs,
        properties=raw_properties,
        projected_crs=feature.projected_crs,
        measurement_status=feature.measurement_status,
        measurements=measurements_model,
        warnings=raw_warnings,
    )


def build_summary(features: list[Any]) -> MeasurementSummary:
    """Build a MeasurementSummary across ALL features of a file (not just the page).

    The caller must pass the complete list, not the page slice.
    """
    total_area = 0.0
    total_length = 0.0
    counts: dict[str, int] = {}

    for feat in features:
        status = feat.measurement_status
        counts[status] = counts.get(status, 0) + 1

        raw = _parse_json_field(feat.measurements)
        if raw:
            a = raw.get("area_m2")
            if a is not None:
                total_area += float(a)
            ln = raw.get("length_m")
            if ln is not None:
                total_length += float(ln)

    return MeasurementSummary(
        total_area_m2=total_area,
        total_length_m=total_length,
        counts_by_status=counts,
    )
