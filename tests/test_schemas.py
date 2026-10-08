"""Unit tests for app/api/schemas.py assembly helpers.

Tests cover:
  - feature_to_measurement_item: geometry included / excluded, JSON parsing,
    measurements typed correctly, warnings list
  - build_summary: area/length totals, counts_by_status, empty feature list
  - _parse_json_field: None, valid JSON, broken JSON
"""

from __future__ import annotations

import json
import types

from app.api.schemas import (
    _parse_json_field,
    build_summary,
    feature_to_measurement_item,
)
from app.db.models import MeasurementStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_feature(
    *,
    feature_index: int = 0,
    geometry_type: str | None = "Polygon",
    geometry: str | None = None,
    crs: str | None = "EPSG:4326",
    properties: str | None = '{"name": "A"}',
    measurement_status: str = MeasurementStatus.COMPLETED,
    measurements: str | None = None,
    projected_crs: str | None = "EPSG:32632",
    warnings: str | None = None,
) -> types.SimpleNamespace:
    """Build a duck-typed Feature-like object without touching the ORM layer.

    feature_to_measurement_item() reads attributes only, so SimpleNamespace
    satisfies the interface without triggering SQLAlchemy instrumentation.
    """
    return types.SimpleNamespace(
        feature_index=feature_index,
        geometry_type=geometry_type,
        geometry=geometry,
        crs=crs,
        properties=properties,
        measurement_status=measurement_status,
        measurements=measurements,
        projected_crs=projected_crs,
        warnings=warnings,
    )


_POLY_GEOJSON = json.dumps(
    {"type": "Polygon", "coordinates": [[[9.0, 51.0], [9.1, 51.0], [9.1, 51.1], [9.0, 51.0]]]}
)

_MEAS_JSON = json.dumps(
    {
        "area_m2": 54321.0,
        "area_ha": 5.4321,
        "perimeter_m": 930.0,
        "length_m": None,
        "length_km": None,
    }
)

_LINE_MEAS_JSON = json.dumps(
    {"area_m2": None, "area_ha": None, "perimeter_m": None, "length_m": 1200.0, "length_km": 1.2}
)


# ---------------------------------------------------------------------------
# _parse_json_field
# ---------------------------------------------------------------------------


class TestParseJsonField:
    def test_none_returns_none(self) -> None:
        assert _parse_json_field(None) is None

    def test_valid_dict(self) -> None:
        assert _parse_json_field('{"a": 1}') == {"a": 1}

    def test_valid_list(self) -> None:
        assert _parse_json_field('["x", "y"]') == ["x", "y"]

    def test_broken_json_returns_none(self) -> None:
        assert _parse_json_field("{not valid}") is None

    def test_empty_string_returns_none(self) -> None:
        result = _parse_json_field("")
        assert result is None


# ---------------------------------------------------------------------------
# feature_to_measurement_item
# ---------------------------------------------------------------------------


class TestFeatureToMeasurementItem:
    def test_geometry_excluded_by_default(self) -> None:
        feat = _make_feature(geometry=_POLY_GEOJSON)
        item = feature_to_measurement_item(feat, include_geometry=False)
        assert item.geometry is None

    def test_geometry_included_when_requested(self) -> None:
        feat = _make_feature(geometry=_POLY_GEOJSON)
        item = feature_to_measurement_item(feat, include_geometry=True)
        assert item.geometry is not None
        assert item.geometry["type"] == "Polygon"

    def test_null_geometry_always_none(self) -> None:
        feat = _make_feature(geometry=None)
        item = feature_to_measurement_item(feat, include_geometry=True)
        assert item.geometry is None

    def test_properties_parsed(self) -> None:
        feat = _make_feature(properties='{"road": "A5", "lanes": 4}')
        item = feature_to_measurement_item(feat, include_geometry=False)
        assert item.properties == {"road": "A5", "lanes": 4}

    def test_null_properties_becomes_empty_dict(self) -> None:
        feat = _make_feature(properties=None)
        item = feature_to_measurement_item(feat, include_geometry=False)
        assert item.properties == {}

    def test_measurements_typed_correctly(self) -> None:
        feat = _make_feature(measurements=_MEAS_JSON)
        item = feature_to_measurement_item(feat, include_geometry=False)
        assert item.measurements is not None
        assert abs(item.measurements.area_m2 - 54321.0) < 0.01
        assert abs(item.measurements.area_ha - 5.4321) < 0.0001
        assert item.measurements.length_m is None

    def test_line_measurements_typed(self) -> None:
        feat = _make_feature(
            geometry_type="LineString",
            measurements=_LINE_MEAS_JSON,
            measurement_status=MeasurementStatus.COMPLETED,
        )
        item = feature_to_measurement_item(feat, include_geometry=False)
        assert item.measurements.area_m2 is None
        assert abs(item.measurements.length_m - 1200.0) < 0.01
        assert abs(item.measurements.length_km - 1.2) < 0.001

    def test_no_measurements_is_none(self) -> None:
        feat = _make_feature(measurement_status=MeasurementStatus.NOT_REQUIRED, measurements=None)
        item = feature_to_measurement_item(feat, include_geometry=False)
        assert item.measurements is None

    def test_warnings_parsed_as_list(self) -> None:
        feat = _make_feature(warnings='["warn 1", "warn 2"]')
        item = feature_to_measurement_item(feat, include_geometry=False)
        assert item.warnings == ["warn 1", "warn 2"]

    def test_null_warnings_becomes_empty_list(self) -> None:
        feat = _make_feature(warnings=None)
        item = feature_to_measurement_item(feat, include_geometry=False)
        assert item.warnings == []

    def test_crs_and_projected_crs_set(self) -> None:
        feat = _make_feature(crs="EPSG:4326", projected_crs="EPSG:32633")
        item = feature_to_measurement_item(feat, include_geometry=False)
        assert item.crs == "EPSG:4326"
        assert item.projected_crs == "EPSG:32633"

    def test_feature_index_preserved(self) -> None:
        feat = _make_feature(feature_index=42)
        item = feature_to_measurement_item(feat, include_geometry=False)
        assert item.feature_index == 42


# ---------------------------------------------------------------------------
# build_summary
# ---------------------------------------------------------------------------


class TestBuildSummary:
    def test_empty_list(self) -> None:
        summary = build_summary([])
        assert summary.total_area_m2 == 0.0
        assert summary.total_length_m == 0.0
        assert summary.counts_by_status == {}

    def test_area_aggregated(self) -> None:
        feats = [
            _make_feature(measurements=_MEAS_JSON),
            _make_feature(measurements=_MEAS_JSON, feature_index=1),
        ]
        summary = build_summary(feats)
        assert abs(summary.total_area_m2 - 2 * 54321.0) < 0.01

    def test_length_aggregated(self) -> None:
        feats = [
            _make_feature(
                geometry_type="LineString",
                measurements=_LINE_MEAS_JSON,
                measurement_status=MeasurementStatus.COMPLETED,
                feature_index=0,
            ),
            _make_feature(
                geometry_type="LineString",
                measurements=_LINE_MEAS_JSON,
                measurement_status=MeasurementStatus.COMPLETED,
                feature_index=1,
            ),
        ]
        summary = build_summary(feats)
        assert abs(summary.total_length_m - 2 * 1200.0) < 0.01

    def test_counts_by_status(self) -> None:
        feats = [
            _make_feature(measurement_status=MeasurementStatus.COMPLETED, feature_index=0),
            _make_feature(measurement_status=MeasurementStatus.COMPLETED, feature_index=1),
            _make_feature(
                measurement_status=MeasurementStatus.NOT_REQUIRED,
                feature_index=2,
                measurements=None,
            ),
            _make_feature(
                measurement_status=MeasurementStatus.EMPTY, feature_index=3, measurements=None
            ),
        ]
        summary = build_summary(feats)
        assert summary.counts_by_status["COMPLETED"] == 2
        assert summary.counts_by_status["NOT_REQUIRED"] == 1
        assert summary.counts_by_status["EMPTY"] == 1

    def test_null_measurements_skipped(self) -> None:
        feats = [_make_feature(measurements=None, measurement_status=MeasurementStatus.EMPTY)]
        summary = build_summary(feats)
        assert summary.total_area_m2 == 0.0
        assert summary.total_length_m == 0.0

    def test_mixed_polygon_and_line(self) -> None:
        feats = [
            _make_feature(measurements=_MEAS_JSON, feature_index=0),  # polygon
            _make_feature(
                measurements=_LINE_MEAS_JSON,
                feature_index=1,  # line
                geometry_type="LineString",
                measurement_status=MeasurementStatus.COMPLETED,
            ),
        ]
        summary = build_summary(feats)
        assert abs(summary.total_area_m2 - 54321.0) < 0.01
        assert abs(summary.total_length_m - 1200.0) < 0.01
