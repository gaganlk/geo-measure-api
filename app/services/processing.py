"""processing.py — full read → measure → persist orchestrator.

Design
------
``FileProcessor`` is the single public surface.  Its ``process()`` method is a
*synchronous* function designed to be called from a ``run_in_executor`` /
``asyncio.to_thread`` call in the route so that GeoPandas / pyproj CPU work
never blocks the event loop.  The DB writes are performed *after* the sync
work returns, inside the async route, keeping the session usage fully async.

The interface is deliberately thin so a background-worker implementation can
satisfy it by providing the same ``process_sync`` signature.

Pipeline
--------
1. ingest_file() — safe ZIP extraction / KML parse → list[FeatureRecord]
2. measure_all() — per-feature CRS + measurement → list[MeasurementOutcome]
3. Persist: update each Feature row with measurement results + warnings
4. Update File status to COMPLETED / FAILED

All exceptions that originate from the user's data are AppError subclasses;
unexpected failures are wrapped in ProcessingError.

Public API
----------
ProcessResult          – dataclass returned by process_sync
FileProcessor          – interface-like class; instantiate with a DB session
FileProcessor.process  – async entry point called from the route
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import shutil
import tempfile

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    FileTooLargeError,
    ProcessingError,
    UnsupportedFileTypeError,
    ValidationError,
    ZipSecurityError,
)
from app.db.models import Feature, File, FileStatus, MeasurementStatus
from app.db.repository import FeatureRepository, FileRepository
from app.services.ingestion import (
    _check_kml_xml,
    _find_shp,
    _safe_extract_zip,
    _validate_shapefile_companions,
)
from app.services.measurement import MeasurementOutcome, measure_all
from app.services.readers import (
    FeatureRecord,
    geometry_to_geojson,
    read_kml,
    read_shapefile,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result dataclass (returned from the sync work, consumed in the async route)
# ---------------------------------------------------------------------------


@dataclass
class ProcessResult:
    """Outcome of the synchronous read+measure step."""

    records: list[FeatureRecord]
    outcomes: list[MeasurementOutcome]
    source_crs: str | None


# ---------------------------------------------------------------------------
# Synchronous worker (no DB access — runs in threadpool)
# ---------------------------------------------------------------------------


def _process_sync(file_path: Path, file_type: str) -> ProcessResult:
    """Read and measure the file contents.  No DB access.

    This is the CPU-bound part of the pipeline.  It is called via
    ``asyncio.to_thread`` so it must be a plain sync function.

    Args:
        file_path: Saved upload path (.zip or .kml).
        file_type: ``"shapefile"`` or ``"kml"``.

    Returns:
        ProcessResult with records, outcomes, and detected source CRS.

    Raises:
        AppError subclasses for user-data problems.
        ProcessingError for unexpected failures (wraps original exc).
    """
    extract_dir: Path | None = None
    try:
        records: list[FeatureRecord]

        if file_type == "shapefile":
            extract_dir = Path(tempfile.mkdtemp(prefix="geo_process_"))
            _safe_extract_zip(file_path, extract_dir)
            shp_path = _find_shp(extract_dir)
            _validate_shapefile_companions(shp_path)
            records = read_shapefile(shp_path)
            source_crs = records[0].source_crs if records else None

        elif file_type == "kml":
            _check_kml_xml(file_path)
            records = read_kml(file_path)
            source_crs = records[0].source_crs if records else "EPSG:4326"

        else:
            raise ProcessingError(f"Unknown file_type '{file_type}'.")

        outcomes = measure_all(records)
        return ProcessResult(records=records, outcomes=outcomes, source_crs=source_crs)

    except (
        UnsupportedFileTypeError,
        FileTooLargeError,
        ValidationError,
        ZipSecurityError,
        ProcessingError,
    ):
        raise  # propagate typed errors unchanged

    except Exception as exc:
        raise ProcessingError(f"Unexpected error while processing file: {exc}") from exc

    finally:
        if extract_dir is not None and extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# ORM assembly helpers
# ---------------------------------------------------------------------------


def _build_feature_row(rec: FeatureRecord, file_id: str) -> Feature:
    """Build a Feature ORM row from a FeatureRecord (no measurement yet)."""
    return Feature(
        file_id=file_id,
        feature_index=rec.index,
        geometry_type=rec.geometry_type,
        geometry=geometry_to_geojson(rec.geometry),
        crs=rec.source_crs,
        properties=json.dumps(rec.properties),
        measurement_status=MeasurementStatus.PENDING,
        measurements=None,
        projected_crs=None,
        warnings=json.dumps(rec.warnings) if rec.warnings else None,
    )


def _apply_outcome(feature: Feature, outcome: MeasurementOutcome) -> None:
    """Write a MeasurementOutcome into an already-tracked Feature row in place."""
    feature.measurement_status = outcome.status
    feature.projected_crs = outcome.projected_crs
    feature.measurements = (
        json.dumps(outcome.measurements) if outcome.measurements is not None else None
    )

    # Merge warnings already on the row with new ones from the outcome
    existing: list[str] = json.loads(feature.warnings) if feature.warnings else []
    all_warnings = existing + outcome.warnings
    feature.warnings = json.dumps(all_warnings) if all_warnings else None


# ---------------------------------------------------------------------------
# FileProcessor — the public interface
# ---------------------------------------------------------------------------


class FileProcessor:
    """Orchestrates read → measure → persist inside a single DB transaction.

    Instantiate once per request.  The ``process`` method is async and
    offloads CPU work to the default threadpool via ``asyncio.to_thread``.

    A background-worker implementation can satisfy the same contract by
    accepting (file_path, file_record, db) and calling ``_process_sync``
    in a worker thread.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._file_repo = FileRepository(db)
        self._feat_repo = FeatureRepository(db)

    async def process(self, file_path: Path, file_record: File) -> File:
        """Run the full pipeline and return the updated File record.

        Args:
            file_path:   Saved upload path.
            file_record: Pre-created ORM File with status=PROCESSING.

        Returns:
            Updated File ORM object (status COMPLETED or FAILED).

        The DB session is flushed but NOT committed here; the caller (route)
        commits via the ``get_db`` dependency context.
        """
        import asyncio

        try:
            # ------------------------------------------------------------------
            # CPU-bound work in threadpool — no DB access inside
            # ------------------------------------------------------------------
            result: ProcessResult = await asyncio.to_thread(
                _process_sync, file_path, file_record.file_type
            )

            # ------------------------------------------------------------------
            # Async DB writes — inside the event loop
            # ------------------------------------------------------------------

            # 1. Insert Feature rows with measurements
            feature_rows: list[Feature] = []
            for rec, outcome in zip(result.records, result.outcomes, strict=True):
                row = _build_feature_row(rec, file_record.id)
                _apply_outcome(row, outcome)
                feature_rows.append(row)

            if feature_rows:
                await self._feat_repo.bulk_create(feature_rows)

            # 2. Mark file COMPLETED
            updated = await self._file_repo.update_status(
                file_record,
                status=FileStatus.COMPLETED,
                feature_count=len(result.records),
                crs=result.source_crs,
            )
            log.info(
                "Processed file %s: %d features, CRS=%s.",
                file_record.id,
                len(result.records),
                result.source_crs,
            )
            return updated

        except (
            UnsupportedFileTypeError,
            FileTooLargeError,
            ValidationError,
            ZipSecurityError,
            ProcessingError,
        ) as exc:
            log.warning("Processing rejected file %s: %s", file_record.id, exc.message)
            await self._file_repo.update_status(
                file_record,
                status=FileStatus.FAILED,
                error=exc.message,
            )
            raise

        except Exception as exc:
            msg = f"Unexpected error: {exc}"
            log.exception("Processing failed for file %s", file_record.id)
            await self._file_repo.update_status(
                file_record,
                status=FileStatus.FAILED,
                error=msg,
            )
            raise ProcessingError(msg) from exc
