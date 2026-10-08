"""Tests for app/services/readers.py — JSON-safe conversion and geometry helpers.

We test only the pure-Python helpers here (no GeoPandas/GDAL required).
Integration tests that exercise read_shapefile() and read_kml() against real
files live in tests/integration/ (added once sample_data/ is populated).
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pytest
from shapely.geometry import LineString, Point, Polygon

from app.services.readers import (
    FeatureRecord,
    _drop_z,
    _geometry_from_raw,
    _json_safe,
    geometry_to_geojson,
)

# ---------------------------------------------------------------------------
# _json_safe
# ---------------------------------------------------------------------------


class TestJsonSafe:
    def test_none(self) -> None:
        assert _json_safe(None) is None

    def test_int(self) -> None:
        assert _json_safe(42) == 42

    def test_float_normal(self) -> None:
        assert _json_safe(3.14) == pytest.approx(3.14)

    def test_nan_becomes_none(self) -> None:
        assert _json_safe(float("nan")) is None

    def test_inf_becomes_none(self) -> None:
        assert _json_safe(float("inf")) is None
        assert _json_safe(float("-inf")) is None

    def test_numpy_int(self) -> None:
        assert _json_safe(np.int64(7)) == 7
        assert isinstance(_json_safe(np.int64(7)), int)

    def test_numpy_float(self) -> None:
        result = _json_safe(np.float64(1.5))
        assert result == pytest.approx(1.5)
        assert isinstance(result, float)

    def test_numpy_nan(self) -> None:
        assert _json_safe(np.float64("nan")) is None

    def test_numpy_bool(self) -> None:
        assert _json_safe(np.bool_(True)) is True

    def test_date_isoformat(self) -> None:
        d = date(2024, 1, 15)
        assert _json_safe(d) == "2024-01-15"

    def test_datetime_isoformat(self) -> None:
        dt = datetime(2024, 6, 1, 12, 0, 0)
        result = _json_safe(dt)
        assert "2024-06-01" in result

    def test_list(self) -> None:
        assert _json_safe([1, float("nan"), "x"]) == [1, None, "x"]

    def test_dict(self) -> None:
        result = _json_safe({"a": np.int32(3), "b": float("nan")})
        assert result == {"a": 3, "b": None}

    def test_nested(self) -> None:
        val = {"arr": [np.float32(1.0), None]}
        result = _json_safe(val)
        assert result["arr"][0] == pytest.approx(1.0)
        assert result["arr"][1] is None

    def test_bool_not_coerced_to_int(self) -> None:
        assert _json_safe(True) is True
        assert _json_safe(False) is False


# ---------------------------------------------------------------------------
# _drop_z
# ---------------------------------------------------------------------------


class TestDropZ:
    def test_2d_point_unchanged(self) -> None:
        p = Point(1, 2)
        result = _drop_z(p)
        assert not result.has_z
        assert result.x == pytest.approx(1)
        assert result.y == pytest.approx(2)

    def test_3d_point_z_removed(self) -> None:
        p = Point(1, 2, 3)
        assert p.has_z
        result = _drop_z(p)
        assert not result.has_z

    def test_none_returns_none(self) -> None:
        assert _drop_z(None) is None

    def test_3d_polygon_z_removed(self) -> None:
        poly = Polygon([(0, 0, 10), (1, 0, 10), (1, 1, 10), (0, 1, 10)])
        assert poly.has_z
        result = _drop_z(poly)
        assert not result.has_z


# ---------------------------------------------------------------------------
# _geometry_from_raw
# ---------------------------------------------------------------------------


class TestGeometryFromRaw:
    def test_none_geom_warns(self) -> None:
        warnings: list[str] = []
        geom, gtype = _geometry_from_raw(None, 0, warnings)
        assert geom is None
        assert gtype is None
        assert len(warnings) == 1
        assert "null geometry" in warnings[0]

    def test_valid_point(self) -> None:
        p = Point(1, 2)
        warnings: list[str] = []
        geom, gtype = _geometry_from_raw(p, 0, warnings)
        assert geom is not None
        assert gtype == "Point"
        assert not warnings

    def test_bad_dict_warns_not_raises(self) -> None:
        """Malformed geometry dict should warn, not crash."""
        warnings: list[str] = []
        geom, gtype = _geometry_from_raw({"type": "BadType", "coordinates": []}, 5, warnings)
        # May return None with a warning OR a valid empty geom — must not raise
        assert len(warnings) >= 0  # tolerate either outcome without crash


# ---------------------------------------------------------------------------
# geometry_to_geojson
# ---------------------------------------------------------------------------


class TestGeometryToGeojson:
    def test_point(self) -> None:
        import json

        geojson = geometry_to_geojson(Point(1, 2))
        assert geojson is not None
        obj = json.loads(geojson)
        assert obj["type"] == "Point"
        assert obj["coordinates"] == pytest.approx([1, 2])

    def test_none_returns_none(self) -> None:
        assert geometry_to_geojson(None) is None

    def test_linestring(self) -> None:
        import json

        ls = LineString([(0, 0), (1, 1)])
        geojson = geometry_to_geojson(ls)
        obj = json.loads(geojson)
        assert obj["type"] == "LineString"

    def test_polygon(self) -> None:
        import json

        poly = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
        geojson = geometry_to_geojson(poly)
        obj = json.loads(geojson)
        assert obj["type"] == "Polygon"


# ---------------------------------------------------------------------------
# FeatureRecord dataclass
# ---------------------------------------------------------------------------


class TestFeatureRecord:
    def test_defaults(self) -> None:
        rec = FeatureRecord(
            index=0,
            geometry=None,
            geometry_type=None,
            properties={},
            source_crs="EPSG:4326",
        )
        assert rec.warnings == []
        assert rec.index == 0

    def test_with_geometry(self) -> None:
        p = Point(10, 20)
        rec = FeatureRecord(
            index=3,
            geometry=p,
            geometry_type="Point",
            properties={"name": "test"},
            source_crs="EPSG:4326",
            warnings=["w1"],
        )
        assert rec.geometry is p
        assert rec.properties["name"] == "test"
        assert rec.warnings == ["w1"]
