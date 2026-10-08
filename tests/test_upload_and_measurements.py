"""Integration tests for POST /api/files/ and GET /api/files/{id}/measurements/.

Strategy
--------
- We do NOT exercise the real GeoPandas / pyogrio stack in these tests.
- FileProcessor.process() is patched with a fixture that inserts real Feature
  rows, so the measurements endpoint can be tested end-to-end against an
  in-memory SQLite DB.
- The upload validation path (wrong extension, magic bytes, too large) is
  tested against the real validate_upload() helper — those have no geo deps.
- A thin "fake processor" inserts a controlled set of Feature rows and marks
  the File COMPLETED so we can assert the full response shape of every
  endpoint without needing GDAL.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
import io
import json
from typing import Any
from unittest.mock import patch
import zipfile

from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.database import Base, get_db
from app.db.models import Feature, File, FileStatus, MeasurementStatus
from app.db.repository import FeatureRepository, FileRepository
from app.main import create_app
from app.services.processing import FileProcessor

# ---------------------------------------------------------------------------
# Helpers — build test payloads
# ---------------------------------------------------------------------------


def _make_zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _minimal_shapefile_zip() -> bytes:
    """ZIP with the four required shapefile components (empty bodies are fine for
    the *validation* path; the reader would fail on empty bodies, but we mock
    FileProcessor.process for integration tests)."""
    return _make_zip_bytes(
        {
            "layer.shp": b"\x00",
            "layer.shx": b"\x00",
            "layer.dbf": b"\x00",
            "layer.prj": b'GEOGCS["GCS_WGS_1984"]',
        }
    )


def _minimal_kml_bytes() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document><name>Test</name></Document>
</kml>"""


# ---------------------------------------------------------------------------
# Fake FileProcessor that bypasses all geo work
# ---------------------------------------------------------------------------


def _make_fake_processor(
    db: AsyncSession,
    *,
    feature_rows: list[dict[str, Any]] | None = None,
) -> FileProcessor:
    """Return a FileProcessor whose process() method inserts synthetic rows."""

    async def _fake_process(file_path: Any, file_record: File) -> File:
        file_repo = FileRepository(db)
        feat_repo = FeatureRepository(db)

        rows_to_insert = feature_rows or []
        orm_features = [
            Feature(
                file_id=file_record.id,
                feature_index=r.get("feature_index", i),
                geometry_type=r.get("geometry_type", "Polygon"),
                geometry=r.get("geometry"),
                crs=r.get("crs", "EPSG:4326"),
                properties=r.get("properties", "{}"),
                measurement_status=r.get("measurement_status", MeasurementStatus.COMPLETED),
                measurements=r.get("measurements"),
                projected_crs=r.get("projected_crs"),
                warnings=r.get("warnings"),
            )
            for i, r in enumerate(rows_to_insert)
        ]
        if orm_features:
            await feat_repo.bulk_create(orm_features)

        return await file_repo.update_status(
            file_record,
            status=FileStatus.COMPLETED,
            feature_count=len(rows_to_insert),
            crs="EPSG:4326",
        )

    processor = FileProcessor.__new__(FileProcessor)
    processor._db = db  # type: ignore[attr-defined]
    processor._file_repo = FileRepository(db)
    processor._feat_repo = FeatureRepository(db)
    processor.process = _fake_process  # type: ignore[method-assign]
    return processor


# ---------------------------------------------------------------------------
# Pytest fixtures — isolated DB per test
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(_engine) -> AsyncGenerator[AsyncSession, None]:
    SessionFactory = async_sessionmaker(_engine, expire_on_commit=False, autoflush=False)
    async with SessionFactory() as session:
        yield session
        await session.rollback()


def _make_client(db_session: AsyncSession, feature_rows: list[dict] | None = None):
    """Return an httpx AsyncClient with DB and FileProcessor overridden."""
    app = create_app()

    async def _get_db_override() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _get_db_override

    # Patch FileProcessor so geo work is skipped
    fake = _make_fake_processor(db_session, feature_rows=feature_rows)

    def _fake_init(self: FileProcessor, db: AsyncSession) -> None:
        self._db = db
        self._file_repo = FileRepository(db)
        self._feat_repo = FeatureRepository(db)
        self.process = fake.process  # type: ignore[method-assign]

    return patch.object(FileProcessor, "__init__", _fake_init)


# ---------------------------------------------------------------------------
# POST /api/files/ — validation rejections (no geo deps needed)
# ---------------------------------------------------------------------------


class TestUploadValidation:
    @pytest.mark.asyncio
    async def test_wrong_extension_returns_415(self, db_session: AsyncSession) -> None:
        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/api/files/",
                files={"file": ("data.geojson", b"{}", "application/json")},
            )
        assert resp.status_code == 415
        assert resp.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"

    @pytest.mark.asyncio
    async def test_zip_with_bad_magic_returns_422(self, db_session: AsyncSession) -> None:
        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/api/files/",
                files={"file": ("data.zip", b"NOTZIP", "application/zip")},
            )
        assert resp.status_code == 422
        data = resp.json()
        assert data["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_kml_binary_garbage_returns_422(self, db_session: AsyncSession) -> None:
        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/api/files/",
                files={
                    "file": (
                        "layer.kml",
                        b"\x01\x02\x03\x04",
                        "application/vnd.google-earth.kml+xml",
                    )
                },
            )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_content_length_too_large_returns_413(self, db_session: AsyncSession) -> None:
        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        zip_bytes = _minimal_shapefile_zip()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/api/files/",
                files={"file": ("data.zip", zip_bytes, "application/zip")},
                headers={"content-length": str(51 * 1024 * 1024)},
            )
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "FILE_TOO_LARGE"

    @pytest.mark.asyncio
    async def test_no_db_record_on_rejection(self, db_session: AsyncSession) -> None:
        """A rejected upload must not create a File row in the DB."""
        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post(
                "/api/files/",
                files={"file": ("bad.geojson", b"{}", "application/json")},
            )
        repo = FileRepository(db_session)
        files = await repo.list_all()
        assert files == []


# ---------------------------------------------------------------------------
# POST /api/files/ — successful upload (fake processor)
# ---------------------------------------------------------------------------


class TestUploadSuccess:
    _FEATURE_ROWS = [
        {
            "feature_index": 0,
            "geometry_type": "Polygon",
            "geometry": json.dumps(
                {
                    "type": "Polygon",
                    "coordinates": [[[9.0, 51.0], [9.1, 51.0], [9.1, 51.1], [9.0, 51.0]]],
                }
            ),
            "crs": "EPSG:4326",
            "properties": json.dumps({"name": "Parcel A"}),
            "measurement_status": MeasurementStatus.COMPLETED,
            "measurements": json.dumps(
                {
                    "area_m2": 54321.0,
                    "area_ha": 5.4321,
                    "perimeter_m": 930.0,
                    "length_m": None,
                    "length_km": None,
                }
            ),
            "projected_crs": "EPSG:32632",
            "warnings": None,
        }
    ]

    @pytest.mark.asyncio
    async def test_zip_upload_returns_201(self, db_session: AsyncSession) -> None:
        with _make_client(db_session, feature_rows=self._FEATURE_ROWS):
            app = create_app()

            async def _db():
                yield db_session

            app.dependency_overrides[get_db] = _db
            fake = _make_fake_processor(db_session, feature_rows=self._FEATURE_ROWS)

            def _fake_init(self, db):
                self._db = db
                self._file_repo = FileRepository(db)
                self._feat_repo = FeatureRepository(db)
                self.process = fake.process

            with patch.object(FileProcessor, "__init__", _fake_init):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as c:
                    resp = await c.post(
                        "/api/files/",
                        files={
                            "file": ("parcels.zip", _minimal_shapefile_zip(), "application/zip")
                        },
                    )

        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "COMPLETED"
        assert "id" in data
        assert data["filename"] == "parcels.zip"

    @pytest.mark.asyncio
    async def test_upload_response_shape(self, db_session: AsyncSession) -> None:
        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        fake = _make_fake_processor(db_session, feature_rows=self._FEATURE_ROWS)

        def _fake_init(self, db):
            self._db = db
            self._file_repo = FileRepository(db)
            self._feat_repo = FeatureRepository(db)
            self.process = fake.process

        with patch.object(FileProcessor, "__init__", _fake_init):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/api/files/",
                    files={"file": ("parcels.zip", _minimal_shapefile_zip(), "application/zip")},
                )

        data = resp.json()
        assert set(data.keys()) >= {"id", "filename", "feature_count", "crs", "status"}

    @pytest.mark.asyncio
    async def test_kml_upload_accepted(self, db_session: AsyncSession) -> None:
        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        fake = _make_fake_processor(db_session, feature_rows=[])

        def _fake_init(self, db):
            self._db = db
            self._file_repo = FileRepository(db)
            self._feat_repo = FeatureRepository(db)
            self.process = fake.process

        with patch.object(FileProcessor, "__init__", _fake_init):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                resp = await c.post(
                    "/api/files/",
                    files={
                        "file": (
                            "layer.kml",
                            _minimal_kml_bytes(),
                            "application/vnd.google-earth.kml+xml",
                        )
                    },
                )

        assert resp.status_code == 201
        assert resp.json()["status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# GET /api/files/{id}/
# ---------------------------------------------------------------------------


class TestGetFile:
    @pytest.mark.asyncio
    async def test_get_existing_file(self, db_session: AsyncSession) -> None:
        # Insert directly
        repo = FileRepository(db_session)
        file = await repo.create(filename="test.zip", file_type="shapefile")
        await db_session.flush()

        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/api/files/{file.id}/")

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == file.id
        assert data["filename"] == "test.zip"
        for key in (
            "id",
            "filename",
            "file_type",
            "feature_count",
            "crs",
            "status",
            "error",
            "created_at",
        ):
            assert key in data

    @pytest.mark.asyncio
    async def test_get_unknown_file(self, db_session: AsyncSession) -> None:
        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/files/00000000000000000000000000000000/")

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# GET /api/files/{id}/measurements/
# ---------------------------------------------------------------------------


class TestGetMeasurements:
    _MEAS = json.dumps(
        {
            "area_m2": 54321.0,
            "area_ha": 5.4321,
            "perimeter_m": 930.0,
            "length_m": None,
            "length_km": None,
        }
    )

    async def _setup(self, db_session: AsyncSession) -> str:
        """Insert a File + 3 Features and return the file ID."""
        file_repo = FileRepository(db_session)
        feat_repo = FeatureRepository(db_session)

        file = await file_repo.create(filename="parcels.zip", file_type="shapefile")
        await file_repo.update_status(
            file, status=FileStatus.COMPLETED, feature_count=3, crs="EPSG:4326"
        )

        features = [
            Feature(
                file_id=file.id,
                feature_index=i,
                geometry_type="Polygon",
                geometry=json.dumps(
                    {
                        "type": "Polygon",
                        "coordinates": [[[9.0, 51.0], [9.1, 51.0], [9.1, 51.1], [9.0, 51.0]]],
                    }
                ),
                crs="EPSG:4326",
                properties=json.dumps({"id": i}),
                measurement_status=MeasurementStatus.COMPLETED,
                measurements=self._MEAS,
                projected_crs="EPSG:32632",
                warnings=None,
            )
            for i in range(3)
        ]
        await feat_repo.bulk_create(features)
        return file.id

    @pytest.mark.asyncio
    async def test_measurements_response_shape(self, db_session: AsyncSession) -> None:
        file_id = await self._setup(db_session)
        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/api/files/{file_id}/measurements/")

        assert resp.status_code == 200
        data = resp.json()
        for key in ("file_id", "total", "limit", "offset", "summary", "items"):
            assert key in data, f"Missing key: {key}"

    @pytest.mark.asyncio
    async def test_measurements_total(self, db_session: AsyncSession) -> None:
        file_id = await self._setup(db_session)
        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/api/files/{file_id}/measurements/")

        assert resp.json()["total"] == 3

    @pytest.mark.asyncio
    async def test_measurements_pagination(self, db_session: AsyncSession) -> None:
        file_id = await self._setup(db_session)
        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/api/files/{file_id}/measurements/?limit=2&offset=0")

        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 2
        assert data["limit"] == 2
        assert data["offset"] == 0

    @pytest.mark.asyncio
    async def test_measurements_offset(self, db_session: AsyncSession) -> None:
        file_id = await self._setup(db_session)
        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/api/files/{file_id}/measurements/?limit=2&offset=2")

        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 1

    @pytest.mark.asyncio
    async def test_geometry_excluded_by_default(self, db_session: AsyncSession) -> None:
        file_id = await self._setup(db_session)
        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/api/files/{file_id}/measurements/")

        items = resp.json()["items"]
        assert all(item["geometry"] is None for item in items)

    @pytest.mark.asyncio
    async def test_geometry_included_when_requested(self, db_session: AsyncSession) -> None:
        file_id = await self._setup(db_session)
        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/api/files/{file_id}/measurements/?include_geometry=true")

        items = resp.json()["items"]
        assert all(item["geometry"] is not None for item in items)
        # Should be a parsed dict, not a string
        assert isinstance(items[0]["geometry"], dict)
        assert "type" in items[0]["geometry"]

    @pytest.mark.asyncio
    async def test_summary_totals(self, db_session: AsyncSession) -> None:
        file_id = await self._setup(db_session)
        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/api/files/{file_id}/measurements/")

        summary = resp.json()["summary"]
        assert "total_area_m2" in summary
        assert "total_length_m" in summary
        assert "counts_by_status" in summary
        # 3 features × 54321 m²
        assert abs(summary["total_area_m2"] - 3 * 54321.0) < 1.0
        assert summary["counts_by_status"]["COMPLETED"] == 3

    @pytest.mark.asyncio
    async def test_item_measurement_shape(self, db_session: AsyncSession) -> None:
        file_id = await self._setup(db_session)
        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/api/files/{file_id}/measurements/")

        item = resp.json()["items"][0]
        assert item["measurement_status"] == "COMPLETED"
        assert item["projected_crs"] == "EPSG:32632"
        meas = item["measurements"]
        assert meas is not None
        assert "area_m2" in meas
        assert "area_ha" in meas
        assert "perimeter_m" in meas
        assert "length_m" in meas
        assert "length_km" in meas

    @pytest.mark.asyncio
    async def test_measurements_not_found(self, db_session: AsyncSession) -> None:
        app = create_app()

        async def _db():
            yield db_session

        app.dependency_overrides[get_db] = _db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/files/00000000000000000000000000000099/measurements/")

        assert resp.status_code == 404
