"""Unit tests for app/services/processing.py.

Tests cover:
  - _build_feature_row: ORM row assembly from FeatureRecord
  - _apply_outcome: MeasurementOutcome → Feature row in-place mutation
  - _process_sync: raises ProcessingError on unknown file_type
  - FileProcessor.process: COMPLETED on success, FAILED on known AppError
    (geo stack mocked so no GDAL needed)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from shapely.geometry import Polygon

from app.db.models import Feature, File, FileStatus, MeasurementStatus
from app.services.measurement import MeasurementOutcome
from app.services.processing import (
    ProcessResult,
    _apply_outcome,
    _build_feature_row,
    _process_sync,
)
from app.services.readers import FeatureRecord

# ---------------------------------------------------------------------------
# _build_feature_row
# ---------------------------------------------------------------------------


class TestBuildFeatureRow:
    def _record(self, **kwargs) -> FeatureRecord:
        defaults = {
            "index": 0,
            "geometry": Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
            "geometry_type": "Polygon",
            "properties": {"name": "test"},
            "source_crs": "EPSG:4326",
            "warnings": [],
        }
        defaults.update(kwargs)
        return FeatureRecord(**defaults)

    def test_file_id_set(self) -> None:
        row = _build_feature_row(self._record(), "abc123")
        assert row.file_id == "abc123"

    def test_feature_index_set(self) -> None:
        row = _build_feature_row(self._record(index=7), "x")
        assert row.feature_index == 7

    def test_properties_json_encoded(self) -> None:
        row = _build_feature_row(self._record(properties={"a": 1}), "x")
        assert json.loads(row.properties) == {"a": 1}

    def test_geometry_geojson(self) -> None:
        row = _build_feature_row(self._record(), "x")
        assert row.geometry is not None
        parsed = json.loads(row.geometry)
        assert parsed["type"] == "Polygon"

    def test_null_geometry_stored_as_none(self) -> None:
        row = _build_feature_row(self._record(geometry=None, geometry_type=None), "x")
        assert row.geometry is None

    def test_initial_status_pending(self) -> None:
        row = _build_feature_row(self._record(), "x")
        assert row.measurement_status == MeasurementStatus.PENDING

    def test_warnings_json_encoded(self) -> None:
        row = _build_feature_row(self._record(warnings=["w1", "w2"]), "x")
        assert json.loads(row.warnings) == ["w1", "w2"]

    def test_empty_warnings_stored_as_none(self) -> None:
        row = _build_feature_row(self._record(warnings=[]), "x")
        assert row.warnings is None


# ---------------------------------------------------------------------------
# _apply_outcome
# ---------------------------------------------------------------------------


class TestApplyOutcome:
    def _empty_feature(self) -> Feature:
        return Feature(
            file_id="x",
            feature_index=0,
            measurement_status=MeasurementStatus.PENDING,
            warnings=None,
        )

    def test_status_updated(self) -> None:
        feat = self._empty_feature()
        _apply_outcome(feat, MeasurementOutcome(status="COMPLETED"))
        assert feat.measurement_status == "COMPLETED"

    def test_projected_crs_set(self) -> None:
        feat = self._empty_feature()
        _apply_outcome(feat, MeasurementOutcome(status="COMPLETED", projected_crs="EPSG:32632"))
        assert feat.projected_crs == "EPSG:32632"

    def test_measurements_json_encoded(self) -> None:
        feat = self._empty_feature()
        meas = {
            "area_m2": 100.0,
            "area_ha": 0.01,
            "perimeter_m": 40.0,
            "length_m": None,
            "length_km": None,
        }
        _apply_outcome(feat, MeasurementOutcome(status="COMPLETED", measurements=meas))
        assert json.loads(feat.measurements) == meas

    def test_none_measurements_stored_as_none(self) -> None:
        feat = self._empty_feature()
        _apply_outcome(feat, MeasurementOutcome(status="NOT_REQUIRED", measurements=None))
        assert feat.measurements is None

    def test_warnings_appended_to_existing(self) -> None:
        feat = self._empty_feature()
        feat.warnings = json.dumps(["existing warning"])
        _apply_outcome(feat, MeasurementOutcome(status="COMPLETED", warnings=["new warning"]))
        merged = json.loads(feat.warnings)
        assert "existing warning" in merged
        assert "new warning" in merged

    def test_new_warnings_only(self) -> None:
        feat = self._empty_feature()
        _apply_outcome(feat, MeasurementOutcome(status="COMPLETED", warnings=["w"]))
        assert json.loads(feat.warnings) == ["w"]

    def test_no_warnings_stores_none(self) -> None:
        feat = self._empty_feature()
        _apply_outcome(feat, MeasurementOutcome(status="COMPLETED", warnings=[]))
        assert feat.warnings is None


# ---------------------------------------------------------------------------
# _process_sync — unknown file_type
# ---------------------------------------------------------------------------


class TestProcessSync:
    def test_unknown_file_type_raises_processing_error(self, tmp_path: Path) -> None:
        from app.core.errors import ProcessingError

        dummy = tmp_path / "dummy.bin"
        dummy.write_bytes(b"")
        with pytest.raises(ProcessingError, match="Unknown file_type"):
            _process_sync(dummy, "geojson")


# ---------------------------------------------------------------------------
# FileProcessor.process — success and FAILED paths (mocked geo stack)
# ---------------------------------------------------------------------------


class TestFileProcessorProcess:
    """These tests mock _process_sync so no GDAL is needed."""

    def _make_file_record(self) -> File:
        return File(
            id="aabbcc",
            filename="test.zip",
            file_type="shapefile",
            status=FileStatus.PROCESSING,
        )

    def _make_result(self) -> ProcessResult:
        rec = FeatureRecord(
            index=0,
            geometry=Polygon([(0, 0), (1, 0), (1, 1)]),
            geometry_type="Polygon",
            properties={},
            source_crs="EPSG:4326",
        )
        outcome = MeasurementOutcome(
            status="COMPLETED",
            projected_crs="EPSG:32632",
            measurements={
                "area_m2": 50.0,
                "area_ha": 0.005,
                "perimeter_m": 30.0,
                "length_m": None,
                "length_km": None,
            },
        )
        return ProcessResult(records=[rec], outcomes=[outcome], source_crs="EPSG:4326")

    @pytest.mark.asyncio
    async def test_process_completed_on_success(self, db_session) -> None:
        from app.services.processing import FileProcessor

        file_record = self._make_file_record()
        db_session.add(file_record)
        await db_session.flush()

        processor = FileProcessor(db_session)

        with patch("app.services.processing._process_sync", return_value=self._make_result()):
            updated = await processor.process(Path("dummy.zip"), file_record)

        assert updated.status == FileStatus.COMPLETED
        assert updated.feature_count == 1
        assert updated.crs == "EPSG:4326"

    @pytest.mark.asyncio
    async def test_process_failed_on_app_error(self, db_session) -> None:
        from app.core.errors import ValidationError
        from app.services.processing import FileProcessor

        file_record = self._make_file_record()
        db_session.add(file_record)
        await db_session.flush()

        processor = FileProcessor(db_session)

        with (
            patch(
                "app.services.processing._process_sync",
                side_effect=ValidationError("bad file"),
            ),
            pytest.raises(ValidationError),
        ):
            await processor.process(Path("dummy.zip"), file_record)

        assert file_record.status == FileStatus.FAILED
        assert "bad file" in file_record.error

    @pytest.mark.asyncio
    async def test_process_failed_wraps_unexpected(self, db_session) -> None:
        from app.core.errors import ProcessingError
        from app.services.processing import FileProcessor

        file_record = self._make_file_record()
        db_session.add(file_record)
        await db_session.flush()

        processor = FileProcessor(db_session)

        with (
            patch(
                "app.services.processing._process_sync",
                side_effect=RuntimeError("unexpected crash"),
            ),
            pytest.raises(ProcessingError),
        ):
            await processor.process(Path("dummy.zip"), file_record)

        assert file_record.status == FileStatus.FAILED


# ---------------------------------------------------------------------------
# Conftest re-use — db_session fixture needed for async tests above
# ---------------------------------------------------------------------------
# The db_session fixture is imported from conftest.py automatically by pytest.
