"""Tests for app/services/crs.py.

Covers:
  - UTM zone arithmetic for standard lat/lon pairs
  - Polar UPS fallback (|lat| > 84)
  - zone-boundary detection
  - Transformer cache (same object returned twice)
  - reproject() round-trip sanity
  - geodesic_area_m2 / geodesic_length_m helpers within 1 % of projected values
    for small shapes (the contractual cross-check)
"""

from __future__ import annotations

import pytest
from shapely.geometry import LineString, Point, Polygon

from app.services.crs import (
    _get_transformer,
    _spans_zone_boundary,
    _utm_epsg_from_lonlat,
    geodesic_area_m2,
    geodesic_length_m,
    reproject,
    utm_crs_for_geometry,
)

# ---------------------------------------------------------------------------
# UTM zone arithmetic
# ---------------------------------------------------------------------------


class TestUtmEpsgFromLonLat:
    @pytest.mark.parametrize(
        "lon, lat, expected",
        [
            # Central meridian of zone 32 (6°E-12°E), northern hemisphere
            (9.0, 51.0, "EPSG:32632"),
            # Zone 1 starts at −180°
            (-179.0, 10.0, "EPSG:32601"),
            # Zone 60 ends at 180°
            (179.0, 10.0, "EPSG:32660"),
            # Southern hemisphere
            (9.0, -10.0, "EPSG:32732"),
            # Equator — north hemisphere wins (lat >= 0)
            (0.0, 0.0, "EPSG:32631"),
            # UTC+0 west boundary (lon = −180)
            (-180.0, 45.0, "EPSG:32601"),
            # Zone 33 northern (12°–18° E)
            (15.0, 60.0, "EPSG:32633"),
            # Zone 44 southern (78°-84° E, south lat)
            (81.0, -20.0, "EPSG:32744"),
        ],
    )
    def test_standard_zones(self, lon: float, lat: float, expected: str) -> None:
        assert _utm_epsg_from_lonlat(lon, lat) == expected

    def test_arctic_ups(self) -> None:
        assert _utm_epsg_from_lonlat(0.0, 85.0) == "EPSG:32661"
        assert _utm_epsg_from_lonlat(90.0, 90.0) == "EPSG:32661"

    def test_antarctic_ups(self) -> None:
        assert _utm_epsg_from_lonlat(0.0, -85.0) == "EPSG:32761"
        assert _utm_epsg_from_lonlat(-90.0, -90.0) == "EPSG:32761"

    def test_boundary_at_84_is_utm(self) -> None:
        """lat == 84 is the last valid UTM latitude (not UPS)."""
        epsg = _utm_epsg_from_lonlat(9.0, 84.0)
        assert epsg.startswith("EPSG:326")  # northern UTM

    def test_boundary_above_84_is_ups(self) -> None:
        epsg = _utm_epsg_from_lonlat(9.0, 84.1)
        assert epsg == "EPSG:32661"


# ---------------------------------------------------------------------------
# Zone boundary detection
# ---------------------------------------------------------------------------


class TestSpansZoneBoundary:
    def test_within_one_zone(self) -> None:
        # Zone 32: 6°–12° E — a 2° range stays inside
        assert not _spans_zone_boundary(7.0, 9.0)

    def test_crosses_boundary(self) -> None:
        # 5° to 13° crosses the 6° and 12° zone boundaries
        assert _spans_zone_boundary(5.0, 13.0)

    def test_exact_boundary_lon(self) -> None:
        # 6.0 is the *start* of zone 32; 6.0 → 11.9 stays inside
        assert not _spans_zone_boundary(6.0, 11.9)

    def test_single_degree_crossing(self) -> None:
        # 5.9° to 6.1° crosses the 6° boundary
        assert _spans_zone_boundary(5.9, 6.1)


# ---------------------------------------------------------------------------
# utm_crs_for_geometry
# ---------------------------------------------------------------------------


class TestUtmCrsForGeometry:
    def test_small_polygon_no_warning(self) -> None:
        # 0.1° × 0.1° polygon well inside zone 32N
        poly = Polygon([(9.0, 51.0), (9.1, 51.0), (9.1, 51.1), (9.0, 51.1)])
        crs, warnings = utm_crs_for_geometry(poly, "EPSG:4326")
        assert crs == "EPSG:32632"
        assert warnings == []

    def test_zone_crossing_polygon_warns(self) -> None:
        # Polygon spanning zones 32 (6°–12°E) and 33 (12°–18°E)
        poly = Polygon([(5.0, 51.0), (19.0, 51.0), (19.0, 52.0), (5.0, 52.0)])
        crs, warnings = utm_crs_for_geometry(poly, "EPSG:4326")
        assert len(warnings) == 1
        assert "zone boundary" in warnings[0].lower()

    def test_southern_hemisphere(self) -> None:
        point = Point(-43.0, -22.0)  # Rio de Janeiro approx
        crs, _ = utm_crs_for_geometry(point, "EPSG:4326")
        assert crs == "EPSG:32723"  # zone 23S

    def test_non_wgs84_source(self) -> None:
        """Geometry in EPSG:32632 (UTM 32N) should still select a valid zone."""
        # Stuttgart approx in EPSG:32632 projected metres
        point = Point(513_000, 5_400_000)
        crs, _ = utm_crs_for_geometry(point, "EPSG:32632")
        assert crs.startswith("EPSG:326")  # northern hemisphere UTM


# ---------------------------------------------------------------------------
# Transformer cache
# ---------------------------------------------------------------------------


class TestTransformerCache:
    def test_same_object_returned(self) -> None:
        t1 = _get_transformer("EPSG:4326", "EPSG:32632")
        t2 = _get_transformer("EPSG:4326", "EPSG:32632")
        assert t1 is t2

    def test_different_pair_different_object(self) -> None:
        t1 = _get_transformer("EPSG:4326", "EPSG:32632")
        t2 = _get_transformer("EPSG:4326", "EPSG:32633")
        assert t1 is not t2


# ---------------------------------------------------------------------------
# reproject round-trip
# ---------------------------------------------------------------------------


class TestReproject:
    def test_identity(self) -> None:
        p = Point(9.0, 51.0)
        result = reproject(p, "EPSG:4326", "EPSG:4326")
        assert result.equals_exact(p, tolerance=1e-9)

    def test_wgs84_to_utm_and_back(self) -> None:
        original = Point(9.0, 51.0)
        projected = reproject(original, "EPSG:4326", "EPSG:32632")
        back = reproject(projected, "EPSG:32632", "EPSG:4326")
        # Round-trip should recover lon/lat within 1e-6 degrees
        assert abs(back.x - original.x) < 1e-6
        assert abs(back.y - original.y) < 1e-6


# ---------------------------------------------------------------------------
# Geodesic cross-check helpers (≤ 1 % tolerance for small shapes)
# ---------------------------------------------------------------------------


class TestGeodesicCrossCheck:
    """
    For small polygons / lines the projected area/length (via UTM) and the
    geodesic area/length (via pyproj.Geod) should agree within 1 %.
    """

    # ~1 km × 1 km square in WGS-84 near Stuttgart (51°N, 9°E)
    _SMALL_POLY_WGS84 = Polygon(
        [
            (9.000, 51.000),
            (9.010, 51.000),
            (9.010, 51.009),
            (9.000, 51.009),
        ]
    )

    _SMALL_LINE_WGS84 = LineString([(9.000, 51.000), (9.010, 51.000)])

    def _projected_area(self, geom_wgs84: Polygon) -> float:
        utm, _ = utm_crs_for_geometry(geom_wgs84, "EPSG:4326")
        proj = reproject(geom_wgs84, "EPSG:4326", utm)
        return proj.area

    def _projected_length(self, geom_wgs84: LineString) -> float:
        utm, _ = utm_crs_for_geometry(geom_wgs84, "EPSG:4326")
        proj = reproject(geom_wgs84, "EPSG:4326", utm)
        return proj.length

    def test_area_within_1_percent(self) -> None:
        geodesic = geodesic_area_m2(self._SMALL_POLY_WGS84)
        projected = self._projected_area(self._SMALL_POLY_WGS84)
        ratio = abs(geodesic - projected) / geodesic
        assert ratio < 0.01, (
            f"Geodesic ({geodesic:.2f} m²) vs projected ({projected:.2f} m²) "
            f"differ by {ratio*100:.2f}% — exceeds 1%"
        )

    def test_length_within_1_percent(self) -> None:
        geodesic = geodesic_length_m(self._SMALL_LINE_WGS84)
        projected = self._projected_length(self._SMALL_LINE_WGS84)
        ratio = abs(geodesic - projected) / geodesic
        assert ratio < 0.01, (
            f"Geodesic ({geodesic:.2f} m) vs projected ({projected:.2f} m) "
            f"differ by {ratio*100:.2f}% — exceeds 1%"
        )

    def test_geodesic_area_positive(self) -> None:
        area = geodesic_area_m2(self._SMALL_POLY_WGS84)
        assert area > 0

    def test_geodesic_length_positive(self) -> None:
        length = geodesic_length_m(self._SMALL_LINE_WGS84)
        assert length > 0
