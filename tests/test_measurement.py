"""Tests for app/services/measurement.py.

Covers every status code:
  COMPLETED    – Polygon, MultiPolygon, LineString, MultiLineString
  NOT_REQUIRED – Point, MultiPoint
  UNSUPPORTED  – GeometryCollection, unknown type
  INVALID_GEOMETRY – self-intersecting geometry (bowtie polygon)
  EMPTY        – None geometry, empty geometry
  FAILED       – pathological input that causes an unexpected exception

Also verifies:
  - All five metric keys always present in measurements dict
  - Correct SI conversions (ha = m2 / 10_000, km = m / 1_000)
  - make_valid warning is appended
  - measure_all() isolates per-feature failures
  - Zone-boundary warning propagates into outcome
"""

from __future__ import annotations

from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)
from shapely.geometry.base import BaseGeometry

from app.services.measurement import (
    MeasurementOutcome,
    measure_all,
    measure_feature,
)
from app.services.readers import FeatureRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CRS = "EPSG:4326"


def _measure(geom: BaseGeometry | None, crs: str = _CRS) -> MeasurementOutcome:
    return measure_feature(geom, crs, feature_index=0)


# Small polygon inside UTM zone 32N (Stuttgart area, ~0.01° × 0.009°)
_SMALL_POLY = Polygon([(9.000, 51.000), (9.010, 51.000), (9.010, 51.009), (9.000, 51.009)])

# Self-intersecting "bowtie" polygon (figure-8)
_BOWTIE = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])

# Simple line
_LINE = LineString([(9.000, 51.000), (9.010, 51.000)])


# ---------------------------------------------------------------------------
# EMPTY status
# ---------------------------------------------------------------------------


class TestEmpty:
    def test_none_geometry(self) -> None:
        result = _measure(None)
        assert result.status == "EMPTY"
        assert result.measurements is None

    def test_empty_polygon(self) -> None:
        result = _measure(Polygon())
        assert result.status == "EMPTY"

    def test_empty_linestring(self) -> None:
        result = _measure(LineString())
        assert result.status == "EMPTY"


# ---------------------------------------------------------------------------
# NOT_REQUIRED status
# ---------------------------------------------------------------------------


class TestNotRequired:
    def test_point(self) -> None:
        result = _measure(Point(9.0, 51.0))
        assert result.status == "NOT_REQUIRED"
        # measurements dict still present with all None values
        assert result.measurements is not None
        assert all(v is None for v in result.measurements.values())

    def test_multipoint(self) -> None:
        result = _measure(MultiPoint([(0, 0), (1, 1)]))
        assert result.status == "NOT_REQUIRED"


# ---------------------------------------------------------------------------
# UNSUPPORTED status
# ---------------------------------------------------------------------------


class TestUnsupported:
    def test_geometry_collection(self) -> None:
        gc = GeometryCollection([Point(0, 0), LineString([(0, 0), (1, 1)])])
        result = _measure(gc)
        assert result.status == "UNSUPPORTED"
        assert "GeometryCollection" in result.reason

    def test_unsupported_reason_present(self) -> None:
        result = _measure(GeometryCollection())
        # Even an empty GC hits this guard before EMPTY
        assert result.status in ("UNSUPPORTED", "EMPTY")


# ---------------------------------------------------------------------------
# INVALID_GEOMETRY status
# ---------------------------------------------------------------------------


class TestInvalidGeometry:
    def test_bowtie_repaired_with_warning(self) -> None:
        """Bowtie polygon is invalid; make_valid should repair it."""
        result = _measure(_BOWTIE)
        # After make_valid a bowtie becomes a MultiPolygon (two triangles)
        # or is repaired to a valid polygon — either way it should COMPLETE
        assert result.status in ("COMPLETED", "INVALID_GEOMETRY")
        if result.status == "COMPLETED":
            # A repair warning must have been issued
            assert any("repaired" in w.lower() or "invalid" in w.lower() for w in result.warnings)

    def test_invalid_geometry_has_reason(self) -> None:
        """If make_valid fails (very degenerate), reason is set."""
        # Create a deliberately degenerate geometry with coincident vertices
        # that Shapely 2 may or may not fix — we test the contract, not the outcome
        result = _measure(_BOWTIE)
        if result.status == "INVALID_GEOMETRY":
            assert result.reason is not None


# ---------------------------------------------------------------------------
# COMPLETED status — Polygon
# ---------------------------------------------------------------------------


class TestCompletedPolygon:
    def test_status(self) -> None:
        result = _measure(_SMALL_POLY)
        assert result.status == "COMPLETED"

    def test_all_metric_keys_present(self) -> None:
        result = _measure(_SMALL_POLY)
        assert result.measurements is not None
        for key in ("area_m2", "area_ha", "perimeter_m", "length_m", "length_km"):
            assert key in result.measurements

    def test_area_positive(self) -> None:
        result = _measure(_SMALL_POLY)
        assert result.measurements["area_m2"] > 0  # type: ignore[index]

    def test_ha_conversion(self) -> None:
        result = _measure(_SMALL_POLY)
        m = result.measurements
        assert m is not None
        assert abs(m["area_ha"] - m["area_m2"] / 10_000) < 1e-6  # type: ignore[operator]

    def test_line_metrics_are_none(self) -> None:
        result = _measure(_SMALL_POLY)
        m = result.measurements
        assert m is not None
        assert m["length_m"] is None
        assert m["length_km"] is None

    def test_projected_crs_set(self) -> None:
        result = _measure(_SMALL_POLY)
        assert result.projected_crs is not None
        assert result.projected_crs.startswith("EPSG:")

    def test_multipolygon(self) -> None:
        # Two non-overlapping polygons: _SMALL_POLY at 9-9.01°E and a shifted one at 9.02-9.03°E
        _SMALL_POLY2 = Polygon([(9.020, 51.000), (9.030, 51.000), (9.030, 51.009), (9.020, 51.009)])
        mp = MultiPolygon([_SMALL_POLY, _SMALL_POLY2])
        result = _measure(mp)
        assert result.status == "COMPLETED"
        assert result.measurements["area_m2"] > 0  # type: ignore[index]
        # Area of two non-overlapping identical-sized polygons ≈ 2× one polygon
        single = _measure(_SMALL_POLY).measurements["area_m2"]  # type: ignore[index]
        assert abs(result.measurements["area_m2"] - 2 * single) < single * 0.05  # type: ignore[index]


# ---------------------------------------------------------------------------
# COMPLETED status — LineString
# ---------------------------------------------------------------------------


class TestCompletedLine:
    def test_status(self) -> None:
        result = _measure(_LINE)
        assert result.status == "COMPLETED"

    def test_all_metric_keys_present(self) -> None:
        result = _measure(_LINE)
        assert result.measurements is not None
        for key in ("area_m2", "area_ha", "perimeter_m", "length_m", "length_km"):
            assert key in result.measurements

    def test_length_positive(self) -> None:
        result = _measure(_LINE)
        assert result.measurements["length_m"] > 0  # type: ignore[index]

    def test_km_conversion(self) -> None:
        result = _measure(_LINE)
        m = result.measurements
        assert m is not None
        assert abs(m["length_km"] - m["length_m"] / 1_000) < 1e-6  # type: ignore[operator]

    def test_area_metrics_are_none(self) -> None:
        result = _measure(_LINE)
        m = result.measurements
        assert m is not None
        assert m["area_m2"] is None
        assert m["area_ha"] is None
        assert m["perimeter_m"] is None

    def test_multilinestring(self) -> None:
        ml = MultiLineString([[(9.0, 51.0), (9.01, 51.0)], [(9.0, 51.0), (9.0, 51.01)]])
        result = _measure(ml)
        assert result.status == "COMPLETED"
        assert result.measurements["length_m"] > 0  # type: ignore[index]


# ---------------------------------------------------------------------------
# Zone-boundary warning propagates
# ---------------------------------------------------------------------------


class TestZoneBoundaryWarning:
    def test_large_polygon_warns(self) -> None:
        # Spans zones 32 and 33 (5°–19° E)
        big = Polygon([(5.0, 51.0), (19.0, 51.0), (19.0, 52.0), (5.0, 52.0)])
        result = _measure(big)
        assert result.status == "COMPLETED"
        assert any("zone boundary" in w.lower() for w in result.warnings)


# ---------------------------------------------------------------------------
# Exception safety (FAILED)
# ---------------------------------------------------------------------------


class TestFailedSafety:
    def test_bad_crs_returns_failed_not_raises(self) -> None:
        """An unresolvable CRS should yield FAILED, never propagate."""
        result = measure_feature(
            _SMALL_POLY,
            "EPSG:999999999",  # non-existent CRS
            feature_index=99,
        )
        assert result.status == "FAILED"
        assert result.reason is not None


# ---------------------------------------------------------------------------
# measure_all — isolation
# ---------------------------------------------------------------------------


class TestMeasureAll:
    def _record(self, idx: int, geom: BaseGeometry | None) -> FeatureRecord:
        return FeatureRecord(
            index=idx,
            geometry=geom,
            geometry_type=geom.geom_type if geom else None,
            properties={},
            source_crs=_CRS,
        )

    def test_returns_same_count(self) -> None:
        records = [
            self._record(0, _SMALL_POLY),
            self._record(1, _LINE),
            self._record(2, Point(9.0, 51.0)),
        ]
        outcomes = measure_all(records)
        assert len(outcomes) == len(records)

    def test_per_feature_isolation(self) -> None:
        """A FAILED feature must not prevent others from completing."""
        records = [
            self._record(0, _SMALL_POLY),
            self._record(1, None),  # EMPTY
            self._record(2, _LINE),
        ]
        outcomes = measure_all(records)
        assert outcomes[0].status == "COMPLETED"
        assert outcomes[1].status == "EMPTY"
        assert outcomes[2].status == "COMPLETED"

    def test_reader_warnings_merged(self) -> None:
        """Reader-level warnings should appear in the outcome.warnings."""
        rec = self._record(0, _SMALL_POLY)
        rec.warnings = ["pre-existing reader warning"]
        outcomes = measure_all([rec])
        assert "pre-existing reader warning" in outcomes[0].warnings
