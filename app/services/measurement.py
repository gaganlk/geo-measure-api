"""measurement.py — per-feature geometry measurement with full error isolation.

Design
------
Measurement is attempted for every feature independently; one bad feature
never fails the file.  The outcome is encoded in a ``MeasurementOutcome``
dataclass that carries a ``status`` string and an optional ``measurements``
dict alongside any warnings.

Status values
~~~~~~~~~~~~~
``COMPLETED``       – measurements computed successfully.
``NOT_REQUIRED``    – Point / MultiPoint (no area or length to measure).
``UNSUPPORTED``     – GeometryCollection or unrecognised geometry type.
``INVALID_GEOMETRY``– Geometry failed shapely.validation.make_valid() or the
                      valid geometry was still unusable.
``EMPTY``           – Geometry is None or .is_empty is True.
``FAILED``          – Unexpected exception (should not happen; logged at ERROR).

Measurement dict keys (all values in SI units, None when inapplicable)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``area_m2``      float | None  – planar area in square metres
``area_ha``      float | None  – planar area in hectares (area_m2 / 10_000)
``perimeter_m``  float | None  – exterior + interior ring perimeters in metres
``length_m``     float | None  – total line length in metres
``length_km``    float | None  – total line length in kilometres

Pipeline
--------
1. ``measure_feature(geometry, source_crs)``
   a. Guard: empty → EMPTY
   b. Guard: validate with shapely; if invalid → try make_valid, add warning
   c. Select per-feature projected CRS via ``crs.utm_crs_for_geometry``
   d. Reproject to projected CRS via ``crs.reproject``
   e. Dispatch to geometry-type handler
   f. Return MeasurementOutcome
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Literal

from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity, make_valid

from app.services import crs as crs_module

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status literals
# ---------------------------------------------------------------------------

MeasurementStatus = Literal[
    "COMPLETED",
    "NOT_REQUIRED",
    "UNSUPPORTED",
    "INVALID_GEOMETRY",
    "EMPTY",
    "FAILED",
]

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

# Geometry type groups
_POLYGON_TYPES = frozenset({"Polygon", "MultiPolygon"})
_LINE_TYPES = frozenset({"LineString", "MultiLineString", "LinearRing"})
_POINT_TYPES = frozenset({"Point", "MultiPoint"})

_M2_TO_HA = 1.0 / 10_000.0
_M_TO_KM = 1.0 / 1_000.0


@dataclass
class MeasurementOutcome:
    """Result of measuring a single feature."""

    status: MeasurementStatus
    projected_crs: str | None = None
    measurements: dict[str, float | None] | None = None
    warnings: list[str] = field(default_factory=list)
    reason: str | None = None  # human-readable explanation for non-COMPLETED statuses


# ---------------------------------------------------------------------------
# Geometry-type dispatchers
# ---------------------------------------------------------------------------


def _measure_polygon(geom: BaseGeometry) -> dict[str, float | None]:
    """Return area_m2, area_ha, perimeter_m for a (Multi)Polygon.

    For MultiPolygon the measurements aggregate all member polygons.
    """
    area = geom.area
    perimeter = geom.length  # Shapely .length = sum of all ring perimeters
    return {
        "area_m2": area,
        "area_ha": area * _M2_TO_HA,
        "perimeter_m": perimeter,
        "length_m": None,
        "length_km": None,
    }


def _measure_line(geom: BaseGeometry) -> dict[str, float | None]:
    """Return length_m, length_km for a (Multi)LineString."""
    length = geom.length
    return {
        "area_m2": None,
        "area_ha": None,
        "perimeter_m": None,
        "length_m": length,
        "length_km": length * _M_TO_KM,
    }


def _null_measurements() -> dict[str, float | None]:
    return {
        "area_m2": None,
        "area_ha": None,
        "perimeter_m": None,
        "length_m": None,
        "length_km": None,
    }


# ---------------------------------------------------------------------------
# Validity repair
# ---------------------------------------------------------------------------


def _ensure_valid(
    geometry: BaseGeometry,
    feature_index: int | None,
    warnings: list[str],
) -> BaseGeometry | None:
    """Return *geometry* if valid; attempt make_valid, add warning, or return None.

    Returns None only when the geometry is still invalid after repair.
    """
    if geometry.is_valid:
        return geometry

    reason = explain_validity(geometry)
    label = f"Feature {feature_index}" if feature_index is not None else "Feature"
    log.debug("%s invalid geometry: %s — attempting make_valid.", label, reason)

    try:
        fixed = make_valid(geometry)
    except Exception as exc:
        warnings.append(f"{label}: geometry invalid ({reason}) and make_valid failed: {exc}.")
        return None

    if not fixed.is_valid or fixed.is_empty:
        warnings.append(
            f"{label}: geometry invalid ({reason}); make_valid produced an unusable result."
        )
        return None

    warnings.append(
        f"{label}: geometry was invalid ({reason}); automatically repaired with make_valid."
    )
    return fixed


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def measure_feature(
    geometry: BaseGeometry | None,
    source_crs: str,
    *,
    feature_index: int | None = None,
) -> MeasurementOutcome:
    """Measure a single geometry and return a MeasurementOutcome.

    The function is **exception-safe**: any unexpected error yields status
    ``FAILED`` rather than propagating.

    Args:
        geometry:      Shapely geometry in *source_crs* coordinates.
        source_crs:    EPSG/PROJ string, e.g. ``"EPSG:4326"``.
        feature_index: Optional index for warning messages.

    Returns:
        ``MeasurementOutcome`` with status, projected_crs, measurements, and
        warnings populated.
    """
    warnings: list[str] = []

    try:
        # ------------------------------------------------------------------
        # Guard 1: empty / null geometry
        # ------------------------------------------------------------------
        if geometry is None or geometry.is_empty:
            return MeasurementOutcome(
                status="EMPTY",
                reason="Geometry is null or empty.",
                warnings=warnings,
            )

        geom_type = geometry.geom_type

        # ------------------------------------------------------------------
        # Guard 2: GeometryCollection (heterogeneous mix — unsupported)
        # ------------------------------------------------------------------
        if geom_type == "GeometryCollection":
            return MeasurementOutcome(
                status="UNSUPPORTED",
                reason=(
                    "GeometryCollection is a heterogeneous mix of geometry types; "
                    "split into individual features for measurement."
                ),
                warnings=warnings,
            )

        # ------------------------------------------------------------------
        # Guard 3: Point / MultiPoint — no meaningful metric
        # ------------------------------------------------------------------
        if geom_type in _POINT_TYPES:
            return MeasurementOutcome(
                status="NOT_REQUIRED",
                reason="Point geometries have no area or length.",
                measurements=_null_measurements(),
                warnings=warnings,
            )

        # ------------------------------------------------------------------
        # Guard 4: Unknown geometry type
        # ------------------------------------------------------------------
        if geom_type not in (_POLYGON_TYPES | _LINE_TYPES):
            return MeasurementOutcome(
                status="UNSUPPORTED",
                reason=f"Unrecognised geometry type '{geom_type}'.",
                warnings=warnings,
            )

        # ------------------------------------------------------------------
        # Step A: validity check + optional repair
        # ------------------------------------------------------------------
        geometry = _ensure_valid(geometry, feature_index, warnings)
        if geometry is None:
            return MeasurementOutcome(
                status="INVALID_GEOMETRY",
                reason="Geometry is invalid and could not be repaired automatically.",
                warnings=warnings,
            )

        # ------------------------------------------------------------------
        # Step B: per-feature projected CRS selection
        # ------------------------------------------------------------------
        projected_crs, crs_warnings = crs_module.utm_crs_for_geometry(geometry, source_crs)
        warnings.extend(crs_warnings)

        # ------------------------------------------------------------------
        # Step C: reproject to selected CRS
        # ------------------------------------------------------------------
        projected_geom = crs_module.reproject(geometry, source_crs, projected_crs)

        # ------------------------------------------------------------------
        # Step D: compute measurements in projected space
        # ------------------------------------------------------------------
        if projected_geom.geom_type in _POLYGON_TYPES:
            measurements = _measure_polygon(projected_geom)
        else:
            measurements = _measure_line(projected_geom)

        return MeasurementOutcome(
            status="COMPLETED",
            projected_crs=projected_crs,
            measurements=measurements,
            warnings=warnings,
        )

    except Exception as exc:
        label = f"Feature {feature_index}" if feature_index is not None else "Feature"
        log.error("%s: unexpected measurement error: %s", label, exc, exc_info=True)
        return MeasurementOutcome(
            status="FAILED",
            reason=f"Unexpected error: {exc}",
            warnings=warnings,
        )


# ---------------------------------------------------------------------------
# Batch helper — wraps measure_feature for a list of FeatureRecords
# ---------------------------------------------------------------------------


def measure_all(
    records: list,  # list[FeatureRecord] — avoid circular import; typed as list
) -> list[MeasurementOutcome]:
    """Measure every feature in *records* individually.

    Args:
        records: List of ``FeatureRecord`` instances from ``readers.py``.

    Returns:
        Parallel list of ``MeasurementOutcome``, one per record.
        A failure in one record never affects the others.
    """
    outcomes: list[MeasurementOutcome] = []
    for rec in records:
        outcome = measure_feature(
            rec.geometry,
            rec.source_crs,
            feature_index=rec.index,
        )
        # Merge reader-level warnings (null geometry etc.) with measurement warnings
        all_warnings = rec.warnings + outcome.warnings
        outcomes.append(
            MeasurementOutcome(
                status=outcome.status,
                projected_crs=outcome.projected_crs,
                measurements=outcome.measurements,
                warnings=all_warnings,
                reason=outcome.reason,
            )
        )
    return outcomes
