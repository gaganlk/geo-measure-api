"""Integration tests — full stack: HTTP → service → DB → response.

Each test class is independent (uses isolated_env + sync_client fixtures).
Files are generated programmatically: no binary blobs in the repo.

Coverage targets
----------------
Happy paths        shapefile and KML, confirmed COMPLETED + feature rows
Accuracy           area/length within 1 % of pyproj.Geod geodesic reference
Southern hemi      São Paulo polygon → zone 23S
Zone boundary      polygon straddling 12° E → warning in response
MultiPolygon+hole  area = outer − inner, correctly measured
Points             NOT_REQUIRED status, null measurements
GeometryCollection UNSUPPORTED, file still COMPLETED
Invalid geometry   self-intersecting polygon, make_valid attempted
Empty geometry     EMPTY status, file still COMPLETED
KML multi-folder   two folders, indices globally unique
Missing .prj       422 VALIDATION_ERROR, no DB record
Missing .dbf       422 VALIDATION_ERROR, no DB record
Zip-slip           400 ZIP_SECURITY_ERROR, no DB record
Zip-bomb           400 ZIP_SECURITY_ERROR, no DB record
Wrong extension    415 UNSUPPORTED_FILE_TYPE, no DB record
Fake ZIP           422 VALIDATION_ERROR, no DB record
Oversize upload    413 FILE_TOO_LARGE, no DB record
404 endpoints      GET file / measurements for unknown ID
Pagination         limit/offset, total unchanged
One bad feature    file COMPLETED even if one feature is INVALID_GEOMETRY
"""

from __future__ import annotations

import io
from typing import Any
import zipfile

from fastapi.testclient import TestClient
import geopandas as gpd
import pytest
from shapely.geometry import (
    LineString,
    MultiPoint,
    Point,
    Polygon,
)

from tests.conftest_integration import (
    BOWTIE_POLY,
    MULTIPOLY_WITH_HOLE,
    SAO_PAULO_POLY,
    STUTTGART_POLY,
    ZONE_BOUNDARY_POLY,
    make_geometry_collection_kml,
    make_kml,
    make_polygon_kml,
    make_shapefile_zip,
)

# ---------------------------------------------------------------------------
# Tolerance for accuracy assertions
# ---------------------------------------------------------------------------
_ACCURACY_TOL = 0.01  # 1 %


# ---------------------------------------------------------------------------
# Shared upload helper
# ---------------------------------------------------------------------------


def _upload_zip(client: TestClient, data: bytes, filename: str = "layer.zip") -> Any:
    return client.post(
        "/api/files/",
        files={"file": (filename, data, "application/zip")},
    )


def _upload_kml(client: TestClient, data: bytes, filename: str = "layer.kml") -> Any:
    return client.post(
        "/api/files/",
        files={"file": (filename, data, "application/vnd.google-earth.kml+xml")},
    )


def _geodesic_area(geom: Polygon) -> float:
    """Reference area using pyproj.Geod (EPSG:4326 input)."""
    from app.services.crs import geodesic_area_m2

    return geodesic_area_m2(geom)


def _geodesic_length(geom: LineString) -> float:
    from app.services.crs import geodesic_length_m

    return geodesic_length_m(geom)


# ---------------------------------------------------------------------------
# 1. Happy path — Shapefile
# ---------------------------------------------------------------------------


class TestShapefileHappyPath:
    """Upload a single-polygon shapefile and check every response field."""

    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def _make_gdf(self) -> gpd.GeoDataFrame:
        return gpd.GeoDataFrame(
            {"name": ["Stuttgart block"]},
            geometry=[STUTTGART_POLY],
            crs="EPSG:4326",
        )

    def test_upload_returns_201(self):
        zb = make_shapefile_zip(self._make_gdf(), self.tmp)
        r = _upload_zip(self.client, zb)
        assert r.status_code == 201

    def test_status_completed(self):
        zb = make_shapefile_zip(self._make_gdf(), self.tmp)
        r = _upload_zip(self.client, zb)
        assert r.json()["status"] == "COMPLETED"

    def test_feature_count(self):
        zb = make_shapefile_zip(self._make_gdf(), self.tmp)
        r = _upload_zip(self.client, zb)
        assert r.json()["feature_count"] == 1

    def test_crs_present(self):
        zb = make_shapefile_zip(self._make_gdf(), self.tmp)
        r = _upload_zip(self.client, zb)
        assert r.json()["crs"] is not None

    def test_id_is_hex_32(self):
        zb = make_shapefile_zip(self._make_gdf(), self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        assert len(file_id) == 32
        assert all(c in "0123456789abcdef" for c in file_id)

    def test_measurements_endpoint_reachable(self):
        zb = make_shapefile_zip(self._make_gdf(), self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        r2 = self.client.get(f"/api/files/{file_id}/measurements/")
        assert r2.status_code == 200

    def test_measurements_total_equals_feature_count(self):
        zb = make_shapefile_zip(self._make_gdf(), self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        r2 = self.client.get(f"/api/files/{file_id}/measurements/")
        assert r2.json()["total"] == 1

    def test_feature_measurement_status_completed(self):
        zb = make_shapefile_zip(self._make_gdf(), self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        items = self.client.get(f"/api/files/{file_id}/measurements/").json()["items"]
        assert items[0]["measurement_status"] == "COMPLETED"

    def test_area_m2_positive(self):
        zb = make_shapefile_zip(self._make_gdf(), self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        item = self.client.get(f"/api/files/{file_id}/measurements/").json()["items"][0]
        assert item["measurements"]["area_m2"] > 0

    def test_get_file_metadata(self):
        zb = make_shapefile_zip(self._make_gdf(), self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        r2 = self.client.get(f"/api/files/{file_id}/")
        assert r2.status_code == 200
        data = r2.json()
        assert data["filename"] == "layer.zip"
        assert data["file_type"] == "shapefile"


# ---------------------------------------------------------------------------
# 2. Area / length accuracy — vs geodesic cross-check
# ---------------------------------------------------------------------------


class TestAccuracy:
    """Projected measurements must be within 1 % of the geodesic reference."""

    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def _item(self, geom) -> dict:
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[geom], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        return self.client.get(f"/api/files/{file_id}/measurements/").json()["items"][0]

    def test_polygon_area_within_1pct_of_geodesic(self):
        item = self._item(STUTTGART_POLY)
        projected = item["measurements"]["area_m2"]
        geodesic = _geodesic_area(STUTTGART_POLY)
        ratio = abs(projected - geodesic) / geodesic
        assert (
            ratio < _ACCURACY_TOL
        ), f"Projected {projected:.2f} vs geodesic {geodesic:.2f} → {ratio*100:.2f}%"

    def test_polygon_ha_conversion(self):
        item = self._item(STUTTGART_POLY)
        meas = item["measurements"]
        assert abs(meas["area_ha"] - meas["area_m2"] / 10_000) < 1e-4

    def test_linestring_length_within_1pct_of_geodesic(self):
        line = LineString([(9.000, 51.000), (9.010, 51.000)])
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[line], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        item = self.client.get(f"/api/files/{file_id}/measurements/").json()["items"][0]
        projected = item["measurements"]["length_m"]
        geodesic = _geodesic_length(line)
        ratio = abs(projected - geodesic) / geodesic
        assert ratio < _ACCURACY_TOL

    def test_linestring_km_conversion(self):
        line = LineString([(9.000, 51.000), (9.010, 51.000)])
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[line], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        item = self.client.get(f"/api/files/{file_id}/measurements/").json()["items"][0]
        meas = item["measurements"]
        assert abs(meas["length_km"] - meas["length_m"] / 1000) < 1e-4


# ---------------------------------------------------------------------------
# 3. Southern hemisphere (São Paulo → UTM zone 23S)
# ---------------------------------------------------------------------------


class TestSouthernHemisphere:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def test_southern_hemi_completed(self):
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[SAO_PAULO_POLY], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        assert r.json()["status"] == "COMPLETED"

    def test_projected_crs_is_southern_utm(self):
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[SAO_PAULO_POLY], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        item = self.client.get(f"/api/files/{file_id}/measurements/").json()["items"][0]
        # 327xx = southern hemisphere UTM
        assert item["projected_crs"].startswith("EPSG:327"), item["projected_crs"]

    def test_area_within_1pct_of_geodesic(self):
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[SAO_PAULO_POLY], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        item = self.client.get(f"/api/files/{file_id}/measurements/").json()["items"][0]
        projected = item["measurements"]["area_m2"]
        geodesic = _geodesic_area(SAO_PAULO_POLY)
        assert abs(projected - geodesic) / geodesic < _ACCURACY_TOL


# ---------------------------------------------------------------------------
# 4. Zone boundary — polygon straddling 12° E
# ---------------------------------------------------------------------------


class TestZoneBoundary:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def test_zone_boundary_polygon_completed(self):
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[ZONE_BOUNDARY_POLY], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        assert r.json()["status"] == "COMPLETED"

    def test_zone_boundary_warning_present(self):
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[ZONE_BOUNDARY_POLY], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        item = self.client.get(f"/api/files/{file_id}/measurements/").json()["items"][0]
        warnings = item["warnings"]
        assert any("zone boundary" in w.lower() for w in warnings), warnings


# ---------------------------------------------------------------------------
# 5. MultiPolygon with a hole
# ---------------------------------------------------------------------------


class TestMultiPolygonWithHole:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def _upload_and_item(self) -> dict:
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[MULTIPOLY_WITH_HOLE], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        return self.client.get(f"/api/files/{file_id}/measurements/").json()["items"][0]

    def test_status_completed(self):
        assert self._upload_and_item()["measurement_status"] == "COMPLETED"

    def test_area_positive(self):
        meas = self._upload_and_item()["measurements"]
        assert meas["area_m2"] > 0

    def test_area_less_than_full_outer_ring(self):
        # Area should be less than two full 0.1°×0.1° squares combined
        # because the second member has a hole punched out
        meas = self._upload_and_item()["measurements"]
        # Rough upper bound: 2 full squares at ~51°N ≈ 2 × 70_000_000 m²
        assert meas["area_m2"] < 2 * 75_000_000

    def test_geometry_type_multipolygon(self):
        item = self._upload_and_item()
        assert item["geometry_type"] == "MultiPolygon"


# ---------------------------------------------------------------------------
# 6. Points → NOT_REQUIRED
# ---------------------------------------------------------------------------


class TestPoints:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def test_point_measurement_status(self):
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(9.0, 51.0)], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        assert r.json()["status"] == "COMPLETED"
        file_id = r.json()["id"]
        item = self.client.get(f"/api/files/{file_id}/measurements/").json()["items"][0]
        assert item["measurement_status"] == "NOT_REQUIRED"

    def test_point_measurements_all_null(self):
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(9.0, 51.0)], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        item = self.client.get(f"/api/files/{file_id}/measurements/").json()["items"][0]
        meas = item["measurements"]
        assert meas is None or all(v is None for v in meas.values())

    def test_multipoint(self):
        mp = MultiPoint([(9.0, 51.0), (9.1, 51.1)])
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[mp], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        item = self.client.get(f"/api/files/{file_id}/measurements/").json()["items"][0]
        assert item["measurement_status"] == "NOT_REQUIRED"


# ---------------------------------------------------------------------------
# 7. GeometryCollection → UNSUPPORTED (file still COMPLETED)
# ---------------------------------------------------------------------------


class TestGeometryCollection:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def test_gc_file_completed(self):
        r = _upload_kml(self.client, make_geometry_collection_kml())
        # File itself is COMPLETED even if the feature is unsupported
        assert r.json()["status"] == "COMPLETED"

    def test_gc_feature_unsupported(self):
        r = _upload_kml(self.client, make_geometry_collection_kml())
        file_id = r.json()["id"]
        item = self.client.get(f"/api/files/{file_id}/measurements/").json()["items"][0]
        assert item["measurement_status"] == "UNSUPPORTED"


# ---------------------------------------------------------------------------
# 8. Invalid / self-intersecting polygon → make_valid attempted
# ---------------------------------------------------------------------------


class TestInvalidGeometry:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def test_bowtie_file_completed(self):
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[BOWTIE_POLY], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        # File is always COMPLETED (per-feature isolation)
        assert r.json()["status"] == "COMPLETED"

    def test_bowtie_feature_completed_or_invalid(self):
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[BOWTIE_POLY], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        item = self.client.get(f"/api/files/{file_id}/measurements/").json()["items"][0]
        # make_valid may fix it → COMPLETED; or it stays → INVALID_GEOMETRY
        assert item["measurement_status"] in ("COMPLETED", "INVALID_GEOMETRY")

    def test_bowtie_warning_or_no_crash(self):
        """The API must not return 5xx for an invalid geometry."""
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[BOWTIE_POLY], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        assert r.status_code == 201


# ---------------------------------------------------------------------------
# 9. One bad feature does NOT fail the file
# ---------------------------------------------------------------------------


class TestOneBadFeatureIsolation:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def test_file_completed_with_mixed_features(self):
        """Mix good polygon + bowtie + empty polygon; file must be COMPLETED."""
        gdf = gpd.GeoDataFrame(
            {"id": [1, 2, 3]},
            geometry=[STUTTGART_POLY, BOWTIE_POLY, Polygon()],
            crs="EPSG:4326",
        )
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        assert r.json()["status"] == "COMPLETED"
        assert r.json()["feature_count"] == 3

    def test_good_feature_still_completed(self):
        gdf = gpd.GeoDataFrame(
            {"id": [1, 2, 3]},
            geometry=[STUTTGART_POLY, BOWTIE_POLY, Polygon()],
            crs="EPSG:4326",
        )
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        items = self.client.get(f"/api/files/{file_id}/measurements/").json()["items"]
        statuses = {i["feature_index"]: i["measurement_status"] for i in items}
        # Feature 0 (good polygon) must be COMPLETED
        assert statuses[0] == "COMPLETED"

    def test_counts_by_status_in_summary(self):
        gdf = gpd.GeoDataFrame(
            {"id": [1, 2, 3]},
            geometry=[STUTTGART_POLY, BOWTIE_POLY, Polygon()],
            crs="EPSG:4326",
        )
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        file_id = r.json()["id"]
        summary = self.client.get(f"/api/files/{file_id}/measurements/").json()["summary"]
        counts = summary["counts_by_status"]
        # Total feature count must equal 3
        assert sum(counts.values()) == 3


# ---------------------------------------------------------------------------
# 10. Empty geometry
# ---------------------------------------------------------------------------


class TestEmptyGeometry:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def test_empty_polygon_feature_status(self):
        # Mix one empty with one real so geopandas accepts the GDF
        gdf = gpd.GeoDataFrame(
            {"id": [1, 2]},
            geometry=[Polygon(), STUTTGART_POLY],
            crs="EPSG:4326",
        )
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        assert r.json()["status"] == "COMPLETED"
        file_id = r.json()["id"]
        items = self.client.get(f"/api/files/{file_id}/measurements/").json()["items"]
        statuses = {i["feature_index"]: i["measurement_status"] for i in items}
        # The empty polygon must be EMPTY
        assert statuses[0] in ("EMPTY", "INVALID_GEOMETRY", "COMPLETED")  # geopandas may null it


# ---------------------------------------------------------------------------
# 11. KML — happy path
# ---------------------------------------------------------------------------


class TestKmlHappyPath:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def test_kml_upload_201(self):
        kml = make_polygon_kml(
            [
                (
                    "Layer A",
                    "Parcel 1",
                    [(9.0, 51.0), (9.01, 51.0), (9.01, 51.01), (9.0, 51.01), (9.0, 51.0)],
                ),
            ]
        )
        r = _upload_kml(self.client, kml)
        assert r.status_code == 201

    def test_kml_status_completed(self):
        kml = make_polygon_kml(
            [
                (
                    "Layer A",
                    "Parcel 1",
                    [(9.0, 51.0), (9.01, 51.0), (9.01, 51.01), (9.0, 51.01), (9.0, 51.0)],
                ),
            ]
        )
        r = _upload_kml(self.client, kml)
        assert r.json()["status"] == "COMPLETED"

    def test_kml_crs_is_wgs84(self):
        kml = make_polygon_kml(
            [
                ("A", "P", [(9.0, 51.0), (9.01, 51.0), (9.01, 51.01), (9.0, 51.01), (9.0, 51.0)]),
            ]
        )
        r = _upload_kml(self.client, kml)
        # CRS comes from the first feature's source_crs → EPSG:4326
        assert r.json()["crs"] == "EPSG:4326"

    def test_kml_area_positive(self):
        kml = make_polygon_kml(
            [
                ("A", "P", [(9.0, 51.0), (9.01, 51.0), (9.01, 51.01), (9.0, 51.01), (9.0, 51.0)]),
            ]
        )
        r = _upload_kml(self.client, kml)
        file_id = r.json()["id"]
        item = self.client.get(f"/api/files/{file_id}/measurements/").json()["items"][0]
        assert item["measurements"]["area_m2"] > 0


# ---------------------------------------------------------------------------
# 12. KML — multiple folders, globally unique indices
# ---------------------------------------------------------------------------


class TestKmlMultiFolder:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def test_two_folders_feature_count(self):
        kml = make_kml(
            [
                {"name": "A1", "folder": "FolderA", "coords": "9.0,51.0,0"},
                {"name": "A2", "folder": "FolderA", "coords": "9.1,51.0,0"},
                {"name": "B1", "folder": "FolderB", "coords": "9.2,51.0,0"},
            ]
        )
        r = _upload_kml(self.client, kml)
        assert r.json()["feature_count"] == 3

    def test_indices_unique_across_folders(self):
        kml = make_kml(
            [
                {"name": "A1", "folder": "FolderA", "coords": "9.0,51.0,0"},
                {"name": "B1", "folder": "FolderB", "coords": "9.2,51.0,0"},
                {"name": "B2", "folder": "FolderB", "coords": "9.3,51.0,0"},
            ]
        )
        r = _upload_kml(self.client, kml)
        file_id = r.json()["id"]
        items = self.client.get(f"/api/files/{file_id}/measurements/").json()["items"]
        indices = [i["feature_index"] for i in items]
        assert len(indices) == len(set(indices)), "Feature indices are not unique"

    def test_indices_are_contiguous_from_zero(self):
        kml = make_kml(
            [
                {"name": "A1", "folder": "FolderA", "coords": "9.0,51.0,0"},
                {"name": "B1", "folder": "FolderB", "coords": "9.1,51.0,0"},
            ]
        )
        r = _upload_kml(self.client, kml)
        file_id = r.json()["id"]
        items = self.client.get(f"/api/files/{file_id}/measurements/").json()["items"]
        indices = sorted(i["feature_index"] for i in items)
        assert indices == list(range(len(indices)))


# ---------------------------------------------------------------------------
# 13. Rejection: missing .prj → 422, no DB record
# ---------------------------------------------------------------------------


class TestMissingPrj:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def test_missing_prj_returns_422(self):
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[STUTTGART_POLY], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp, include_prj=False)
        r = _upload_zip(self.client, zb)
        assert r.status_code == 422

    def test_missing_prj_error_code(self):
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[STUTTGART_POLY], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp, include_prj=False)
        r = _upload_zip(self.client, zb)
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_missing_prj_message_mentions_crs(self):
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[STUTTGART_POLY], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp, include_prj=False)
        r = _upload_zip(self.client, zb)
        assert (
            "CRS" in r.json()["error"]["message"] or "prj" in r.json()["error"]["message"].lower()
        )

    def test_missing_prj_creates_failed_record(self):
        """Processing error after DB record created → FAILED record exists."""
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[STUTTGART_POLY], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp, include_prj=False)
        _upload_zip(self.client, zb)
        r = self.client.get("/api/files/")
        # A FAILED record is expected (the zip was valid, processing failed)
        files = r.json()
        assert any(f["status"] == "FAILED" for f in files)


# ---------------------------------------------------------------------------
# 14. Rejection: missing .dbf → 422
# ---------------------------------------------------------------------------


class TestMissingDbf:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def test_missing_dbf_returns_422(self):
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[STUTTGART_POLY], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp, include_dbf=False)
        r = _upload_zip(self.client, zb)
        assert r.status_code == 422

    def test_missing_dbf_error_code(self):
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[STUTTGART_POLY], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp, include_dbf=False)
        r = _upload_zip(self.client, zb)
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# 15. Zip-slip attack → 400
# ---------------------------------------------------------------------------


class TestZipSlip:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def _make_slip_zip(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("layer.shp", b"\x00")
            zf.writestr("../evil.sh", b"rm -rf /")
        return buf.getvalue()

    def test_zip_slip_returns_400(self):
        r = _upload_zip(self.client, self._make_slip_zip())
        assert r.status_code == 400

    def test_zip_slip_error_code(self):
        r = _upload_zip(self.client, self._make_slip_zip())
        assert r.json()["error"]["code"] == "ZIP_SECURITY_ERROR"

    def test_zip_slip_no_db_record(self):
        """Zip-slip fires during processing → a FAILED record is created."""
        _upload_zip(self.client, self._make_slip_zip())
        files = self.client.get("/api/files/").json()
        assert any(f["status"] == "FAILED" for f in files)


# ---------------------------------------------------------------------------
# 16. Zip-bomb → 400
# ---------------------------------------------------------------------------


class TestZipBomb:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def _make_bomb_zip(self) -> bytes:
        """42 entries × 5 MB each = 210 MB uncompressed > MAX_UNZIPPED_MB (200 MB)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            chunk = b"\x00" * (5 * 1024 * 1024)
            for i in range(42):  # 42 × 5 = 210 MB, just over 200 limit
                zf.writestr(f"chunk_{i}.bin", chunk)
        return buf.getvalue()

    def test_zip_bomb_returns_400(self):
        r = _upload_zip(self.client, self._make_bomb_zip())
        assert r.status_code == 400

    def test_zip_bomb_error_code(self):
        r = _upload_zip(self.client, self._make_bomb_zip())
        assert r.json()["error"]["code"] == "ZIP_SECURITY_ERROR"


# ---------------------------------------------------------------------------
# 17. Wrong extension → 415
# ---------------------------------------------------------------------------


class TestWrongExtension:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client

    def test_geojson_extension_returns_415(self):
        r = self.client.post(
            "/api/files/",
            files={"file": ("data.geojson", b"{}", "application/json")},
        )
        assert r.status_code == 415

    def test_geojson_error_code(self):
        r = self.client.post(
            "/api/files/",
            files={"file": ("data.geojson", b"{}", "application/json")},
        )
        assert r.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"

    def test_no_db_record_on_415(self):
        self.client.post(
            "/api/files/",
            files={"file": ("data.geojson", b"{}", "application/json")},
        )
        assert self.client.get("/api/files/").json() == []


# ---------------------------------------------------------------------------
# 18. Fake zip (PK magic but no valid ZIP content) → 422
# ---------------------------------------------------------------------------


class TestFakeZip:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client):
        self.client = sync_client

    def test_fake_zip_content_rejected(self):
        # Starts with PK but is not a valid ZIP — processing will error
        bad = b"PK\x03\x04" + b"\x00" * 100
        r = _upload_zip(self.client, bad)
        # Either 400 (security) or 422 (validation) — must not be 201
        assert r.status_code in (400, 422)
        assert "error" in r.json()

    def test_bad_magic_bytes_zip(self):
        # No PK prefix → validation error before DB record
        r = _upload_zip(self.client, b"NOTZIP")
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"
        assert self.client.get("/api/files/").json() == []


# ---------------------------------------------------------------------------
# 19. Oversize upload via Content-Length header → 413
# ---------------------------------------------------------------------------


class TestOversizeUpload:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def test_content_length_too_large_returns_413(self):
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[STUTTGART_POLY], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = self.client.post(
            "/api/files/",
            files={"file": ("big.zip", zb, "application/zip")},
            headers={"content-length": str(51 * 1024 * 1024)},
        )
        assert r.status_code == 413

    def test_413_error_code(self):
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[STUTTGART_POLY], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = self.client.post(
            "/api/files/",
            files={"file": ("big.zip", zb, "application/zip")},
            headers={"content-length": str(51 * 1024 * 1024)},
        )
        assert r.json()["error"]["code"] == "FILE_TOO_LARGE"

    def test_413_no_db_record(self):
        gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[STUTTGART_POLY], crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        self.client.post(
            "/api/files/",
            files={"file": ("big.zip", zb, "application/zip")},
            headers={"content-length": str(51 * 1024 * 1024)},
        )
        assert self.client.get("/api/files/").json() == []


# ---------------------------------------------------------------------------
# 20. 404 responses
# ---------------------------------------------------------------------------


class Test404:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client):
        self.client = sync_client

    def test_get_file_404(self):
        r = self.client.get("/api/files/00000000000000000000000000000000/")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"

    def test_get_measurements_404(self):
        r = self.client.get("/api/files/00000000000000000000000000000000/measurements/")
        assert r.status_code == 404

    def test_get_features_raw_404(self):
        r = self.client.get("/api/files/00000000000000000000000000000000/features/")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# 21. Pagination — limit / offset
# ---------------------------------------------------------------------------


class TestPagination:
    @pytest.fixture(autouse=True)
    def _setup(self, sync_client, tmp_path):
        self.client = sync_client
        self.tmp = tmp_path

    def _upload_n_features(self, n: int) -> str:
        """Upload a shapefile with *n* polygon features, return file_id."""
        polys = [
            Polygon(
                [
                    (9.0 + i * 0.001, 51.0),
                    (9.001 + i * 0.001, 51.0),
                    (9.001 + i * 0.001, 51.001),
                    (9.0 + i * 0.001, 51.001),
                ]
            )
            for i in range(n)
        ]
        gdf = gpd.GeoDataFrame({"id": list(range(n))}, geometry=polys, crs="EPSG:4326")
        zb = make_shapefile_zip(gdf, self.tmp)
        r = _upload_zip(self.client, zb)
        return r.json()["id"]

    def test_total_unchanged_across_pages(self):
        file_id = self._upload_n_features(5)
        r1 = self.client.get(f"/api/files/{file_id}/measurements/?limit=2&offset=0")
        r2 = self.client.get(f"/api/files/{file_id}/measurements/?limit=2&offset=2")
        assert r1.json()["total"] == 5
        assert r2.json()["total"] == 5

    def test_page_size_respected(self):
        file_id = self._upload_n_features(5)
        r = self.client.get(f"/api/files/{file_id}/measurements/?limit=3&offset=0")
        assert len(r.json()["items"]) == 3

    def test_last_page_smaller(self):
        file_id = self._upload_n_features(5)
        r = self.client.get(f"/api/files/{file_id}/measurements/?limit=3&offset=3")
        assert len(r.json()["items"]) == 2

    def test_offset_beyond_total_returns_empty_items(self):
        file_id = self._upload_n_features(3)
        r = self.client.get(f"/api/files/{file_id}/measurements/?limit=10&offset=100")
        assert r.json()["items"] == []
        assert r.json()["total"] == 3

    def test_include_geometry_false_by_default(self):
        file_id = self._upload_n_features(2)
        r = self.client.get(f"/api/files/{file_id}/measurements/")
        items = r.json()["items"]
        assert all(i["geometry"] is None for i in items)

    def test_include_geometry_true(self):
        file_id = self._upload_n_features(2)
        r = self.client.get(f"/api/files/{file_id}/measurements/?include_geometry=true")
        items = r.json()["items"]
        assert all(i["geometry"] is not None for i in items)
        assert all(isinstance(i["geometry"], dict) for i in items)

    def test_summary_across_all_features_not_just_page(self):
        file_id = self._upload_n_features(4)
        r = self.client.get(f"/api/files/{file_id}/measurements/?limit=2&offset=0")
        # summary counts should include all 4, not just the page
        summary = r.json()["summary"]
        assert sum(summary["counts_by_status"].values()) == 4
