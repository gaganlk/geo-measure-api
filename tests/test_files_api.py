"""Tests for the /api/files/ endpoints (GET list, GET detail, GET measurements)."""

from __future__ import annotations

from unittest.mock import PropertyMock, patch

from httpx import AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.errors import ValidationError
from app.db.models import Feature, FileStatus, MeasurementStatus
from app.db.repository import FeatureRepository, FileRepository

# ---------------------------------------------------------------------------
# List files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_files_empty(client: AsyncClient) -> None:
    response = await client.get("/api/files/")
    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# Get file — 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_file_not_found(client: AsyncClient) -> None:
    response = await client.get("/api/files/doesnotexist/")
    assert response.status_code == 404
    data = response.json()
    assert data["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Get measurements — 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_measurements_file_not_found(client: AsyncClient) -> None:
    response = await client.get("/api/files/doesnotexist/measurements/")
    assert response.status_code == 404
    data = response.json()
    assert data["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# List files — populated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_files_populated(client: AsyncClient, db_session: AsyncSession) -> None:
    file_repo = FileRepository(db_session)
    await file_repo.create(filename="parcels.zip", file_type="shapefile")
    await file_repo.create(filename="parcels.kml", file_type="kml")
    await db_session.flush()

    response = await client.get("/api/files/")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert {d["filename"] for d in data} == {"parcels.zip", "parcels.kml"}


# ---------------------------------------------------------------------------
# List raw features
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_features_populated(client: AsyncClient, db_session: AsyncSession) -> None:
    file_repo = FileRepository(db_session)
    feat_repo = FeatureRepository(db_session)

    file_rec = await file_repo.create(filename="parcels.zip", file_type="shapefile")
    await file_repo.update_status(file_rec, status=FileStatus.COMPLETED, feature_count=2)

    features = [
        Feature(
            file_id=file_rec.id,
            feature_index=0,
            geometry_type="Polygon",
            geometry='{"type": "Polygon", "coordinates": []}',
            crs="EPSG:4326",
            properties='{"name": "A"}',
            measurement_status=MeasurementStatus.COMPLETED,
            measurements='{"area_m2": 100.0}',
            projected_crs="EPSG:32632",
        ),
        Feature(
            file_id=file_rec.id,
            feature_index=1,
            geometry_type="LineString",
            geometry='{"type": "LineString", "coordinates": []}',
            crs="EPSG:4326",
            properties='{"name": "B"}',
            measurement_status=MeasurementStatus.COMPLETED,
            measurements='{"length_m": 50.0}',
            projected_crs="EPSG:32632",
        ),
    ]
    await feat_repo.bulk_create(features)

    response = await client.get(f"/api/files/{file_rec.id}/features/")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert data[0]["feature_index"] == 0
    assert data[1]["feature_index"] == 1


@pytest.mark.asyncio
async def test_upload_streaming_size_exceeded(client: AsyncClient) -> None:
    """When chunks stream to disk and exceed max_upload_bytes, return 413."""
    mock_settings = get_settings()
    with (
        patch("app.api.routes.files._CHUNK", 10),
        patch.object(
            type(mock_settings), "max_upload_bytes", new_callable=PropertyMock, return_value=15
        ),
    ):
        response = await client.post(
            "/api/files/",
            files={
                "file": (
                    "test.kml",
                    b"<kml><Document>streaming body exceeding fifteen bytes limit</Document></kml>",
                    "application/vnd.google-earth.kml+xml",
                )
            },
            headers={"content-length": "5"},  # spoofed small Content-Length header
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


@pytest.mark.asyncio
async def test_upload_processor_app_error_handled(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """When processor encounters AppError during process(), FAILED record is committed."""
    with patch(
        "app.services.processing.read_kml",
        side_effect=ValidationError("Corrupt geometry structure"),
    ):
        response = await client.post(
            "/api/files/",
            files={
                "file": (
                    "parcels.kml",
                    b"<kml><Document/></kml>",
                    "application/vnd.google-earth.kml+xml",
                )
            },
            headers={"content-length": "invalid-header"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    # Verify that the file record in DB has status FAILED
    file_repo = FileRepository(db_session)
    files = await file_repo.list_all()
    assert len(files) == 1
    assert files[0].status == FileStatus.FAILED


# ---------------------------------------------------------------------------
# Measurements & Upload Success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_measurements_populated(client: AsyncClient, db_session: AsyncSession) -> None:
    file_repo = FileRepository(db_session)
    feat_repo = FeatureRepository(db_session)

    file_rec = await file_repo.create(filename="parcels.zip", file_type="shapefile")
    await file_repo.update_status(
        file_rec, status=FileStatus.COMPLETED, feature_count=1, crs="EPSG:4326"
    )

    features = [
        Feature(
            file_id=file_rec.id,
            feature_index=0,
            geometry_type="Polygon",
            geometry='{"type": "Polygon", "coordinates": [[[0,0],[1,0],[1,1],[0,0]]]}',
            crs="EPSG:4326",
            properties='{"name": "Parcel 1"}',
            measurement_status=MeasurementStatus.COMPLETED,
            measurements='{"area_m2": 1000.0, "perimeter_m": 120.0}',
            projected_crs="EPSG:32632",
        )
    ]
    await feat_repo.bulk_create(features)

    # test include_geometry=False
    resp = await client.get(f"/api/files/{file_rec.id}/measurements/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["geometry"] is None

    # test include_geometry=True
    resp_geom = await client.get(f"/api/files/{file_rec.id}/measurements/?include_geometry=true")
    assert resp_geom.status_code == 200
    data_geom = resp_geom.json()
    assert data_geom["items"][0]["geometry"] is not None


@pytest.mark.asyncio
async def test_upload_first_chunk_size_exceeded(client: AsyncClient) -> None:
    """When the first written chunk itself exceeds max_upload_bytes, return 413."""
    mock_settings = get_settings()
    with patch.object(
        type(mock_settings), "max_upload_bytes", new_callable=PropertyMock, return_value=10
    ):
        response = await client.post(
            "/api/files/",
            files={
                "file": (
                    "test.kml",
                    b"<kml><Document>first chunk is already more than 10 bytes</Document></kml>",
                    "application/vnd.google-earth.kml+xml",
                )
            },
            headers={"content-length": "5"},
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


@pytest.mark.asyncio
async def test_direct_route_handlers(db_session: AsyncSession) -> None:
    from app.api.routes.files import get_file, get_measurements, list_features, list_files

    file_repo = FileRepository(db_session)
    feat_repo = FeatureRepository(db_session)

    file_rec = await file_repo.create(filename="parcels.zip", file_type="shapefile")
    await file_repo.update_status(
        file_rec, status=FileStatus.COMPLETED, feature_count=1, crs="EPSG:4326"
    )

    features = [
        Feature(
            file_id=file_rec.id,
            feature_index=0,
            geometry_type="Polygon",
            geometry='{"type": "Polygon", "coordinates": [[[0,0],[1,0],[1,1],[0,0]]]}',
            crs="EPSG:4326",
            properties='{"name": "Parcel 1"}',
            measurement_status=MeasurementStatus.COMPLETED,
            measurements='{"area_m2": 1000.0, "perimeter_m": 120.0}',
            projected_crs="EPSG:32632",
        )
    ]
    await feat_repo.bulk_create(features)

    # 1. list_files direct
    listed = await list_files(limit=100, offset=0, db=db_session)
    assert any(f.id == file_rec.id for f in listed)

    # 2. get_file direct
    got = await get_file(file_id=file_rec.id, db=db_session)
    assert got.id == file_rec.id

    # 3. get_measurements direct
    meas = await get_measurements(
        file_id=file_rec.id, limit=10, offset=0, include_geometry=True, db=db_session
    )
    assert meas.total == 1
    assert meas.items[0].geometry is not None

    # 4. list_features direct
    raw_feats = await list_features(file_id=file_rec.id, limit=10, offset=0, db=db_session)
    assert len(raw_feats) == 1


@pytest.mark.asyncio
async def test_direct_upload_file(db_session: AsyncSession) -> None:
    import io

    from fastapi import Request
    from fastapi import UploadFile as FastAPIUploadFile

    from app.api.routes.files import upload_file

    kml_bytes = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>Test</name>
      <Point><coordinates>10.0,50.0,0</coordinates></Point>
    </Placemark>
  </Document>
</kml>"""
    upload = FastAPIUploadFile(file=io.BytesIO(kml_bytes), filename="test.kml")

    scope = {"type": "http", "headers": [(b"content-length", str(len(kml_bytes)).encode())]}
    request = Request(scope)

    resp = await upload_file(request=request, file=upload, db=db_session)
    assert resp.status == "COMPLETED"
    assert resp.filename == "test.kml"
