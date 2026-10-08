"""routes/files.py — file upload and measurement retrieval endpoints.

Endpoints
---------
POST   /api/v1/files/                      Upload a Shapefile ZIP or KML → 201
GET    /api/v1/files/{file_id}/            File metadata → 200 / 404
GET    /api/v1/files/{file_id}/measurements/  Paginated measurement results → 200 / 404

Design notes
------------
- POST is a *def* (sync) function so FastAPI runs it in the threadpool.
  GeoPandas / pyproj CPU work happens inside FileProcessor.process(), which
  itself calls asyncio.to_thread for the pure-CPU part, so the event loop
  is never blocked.

  Actually: we make POST async and call FileProcessor.process (which uses
  asyncio.to_thread internally) so that we can await DB operations properly.

- File bytes are streamed chunk-by-chunk; Content-Length is checked before
  saving.  Magic-byte validation happens on the first chunk.

- No DB record is created for rejected uploads (wrong extension, too large,
  bad magic bytes).  The record is created only when we are about to start
  processing, so a FAILED record in the DB always means "the file was accepted
  but processing failed", not "invalid upload".

- include_geometry query parameter (default false) controls whether GeoJSON
  geometry is included in measurement items.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, Query, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    ErrorResponse,
    FeatureResponse,
    FileResponse,
    FileUploadResponse,
    MeasurementsResponse,
    build_summary,
    feature_to_measurement_item,
)
from app.config import get_settings
from app.core.errors import AppError
from app.db.database import get_db
from app.db.repository import FeatureRepository, FileRepository
from app.services.ingestion import validate_upload
from app.services.processing import FileProcessor

log = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])

# ---------------------------------------------------------------------------
# Shared query parameter aliases
# ---------------------------------------------------------------------------

LimitQ = Annotated[int, Query(ge=1, le=500, description="Maximum items to return.")]
OffsetQ = Annotated[int, Query(ge=0, description="Number of items to skip.")]


# ---------------------------------------------------------------------------
# POST /files/
# ---------------------------------------------------------------------------

_CHUNK = 64 * 1024  # 64 KB read buffer


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    response_model=FileUploadResponse,
    responses={
        201: {
            "description": "File accepted and processed successfully.",
            "content": {
                "application/json": {
                    "example": {
                        "id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
                        "filename": "parcels.zip",
                        "feature_count": 512,
                        "crs": "EPSG:32632",
                        "status": "COMPLETED",
                    }
                }
            },
        },
        400: {"model": ErrorResponse, "description": "ZIP security violation."},
        413: {"model": ErrorResponse, "description": "File too large."},
        415: {"model": ErrorResponse, "description": "Unsupported file type."},
        422: {"model": ErrorResponse, "description": "Validation or processing error."},
    },
    summary="Upload a Shapefile ZIP or KML",
    description=(
        "Upload a zipped Shapefile (`.zip`) or KML (`.kml`) file.  "
        "The service extracts features, selects a per-feature UTM projection, "
        "and returns area / length measurements synchronously.  "
        "\n\n"
        "**Limits**: "
        "upload size ≤ `MAX_UPLOAD_MB` (default 50 MB), "
        "ZIP entries ≤ `MAX_ZIP_ENTRIES` (default 50), "
        "unzipped size ≤ `MAX_UNZIPPED_MB` (default 200 MB).  "
        "\n\n"
        "Rejected uploads (wrong extension, bad magic bytes, too large) do "
        "**not** create a database record.  Only accepted uploads that fail "
        "during processing create a `FAILED` record."
    ),
)
async def upload_file(
    request: Request,
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
) -> FileUploadResponse:
    """Validate, ingest, and measure a Shapefile ZIP or KML upload."""
    settings = get_settings()

    # ------------------------------------------------------------------
    # Pre-flight: extension + Content-Length header (cheap, no disk write)
    # ------------------------------------------------------------------
    content_length: int | None = None
    raw_cl = request.headers.get("content-length")
    if raw_cl and raw_cl.isdigit():
        content_length = int(raw_cl)

    first_chunk = await file.read(_CHUNK)
    # validate_upload raises AppError subclasses on bad input
    file_type = validate_upload(
        file.filename or "upload",
        first_chunk,
        content_length,
    )

    # ------------------------------------------------------------------
    # Stream to disk — enforce byte limit during write
    # ------------------------------------------------------------------
    suffix = Path(file.filename or "upload").suffix.lower()
    upload_path = settings.UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    bytes_written = 0
    try:
        with upload_path.open("wb") as fh:
            fh.write(first_chunk)
            bytes_written += len(first_chunk)
            if bytes_written > settings.max_upload_bytes:
                fh.close()
                upload_path.unlink(missing_ok=True)
                from app.core.errors import FileTooLargeError

                raise FileTooLargeError(
                    f"File exceeds maximum upload size of {settings.MAX_UPLOAD_MB} MB."
                )

            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > settings.max_upload_bytes:
                    fh.close()
                    upload_path.unlink(missing_ok=True)
                    from app.core.errors import FileTooLargeError

                    raise FileTooLargeError(
                        f"File exceeds maximum upload size of {settings.MAX_UPLOAD_MB} MB."
                    )
                fh.write(chunk)
    except AppError:
        upload_path.unlink(missing_ok=True)
        raise

    # ------------------------------------------------------------------
    # Create DB record — only now, so rejected uploads leave no trace
    # ------------------------------------------------------------------
    file_repo = FileRepository(db)
    file_record = await file_repo.create(
        filename=file.filename or "upload",
        file_type=file_type,
    )

    # ------------------------------------------------------------------
    # Process (read + measure + persist) — CPU in threadpool via to_thread
    # ------------------------------------------------------------------
    processor = FileProcessor(db)
    try:
        updated = await processor.process(upload_path, file_record)
    except AppError:
        # Commit the FAILED file record before propagating the error response
        await db.commit()
        raise
    finally:
        # Always remove the upload; the DB has everything we need
        upload_path.unlink(missing_ok=True)

    return FileUploadResponse.model_validate(updated)


# ---------------------------------------------------------------------------
# GET /files/{file_id}/
# ---------------------------------------------------------------------------


@router.get(
    "/{file_id}/",
    response_model=FileResponse,
    responses={
        200: {
            "description": "File metadata.",
            "content": {
                "application/json": {
                    "example": {
                        "id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
                        "filename": "parcels.zip",
                        "file_type": "shapefile",
                        "feature_count": 512,
                        "crs": "EPSG:32632",
                        "status": "COMPLETED",
                        "error": None,
                        "created_at": "2024-06-01T08:00:00Z",
                    }
                }
            },
        },
        404: {"model": ErrorResponse, "description": "File not found."},
    },
    summary="Get file metadata",
    description="Return metadata for a previously uploaded file by its UUID hex ID.",
)
async def get_file(
    file_id: str,
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """Return metadata for a single uploaded file."""
    repo = FileRepository(db)
    file = await repo.get_or_404(file_id)
    return FileResponse.model_validate(file)


# ---------------------------------------------------------------------------
# GET /files/{file_id}/measurements/
# ---------------------------------------------------------------------------


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    responses={
        200: {"description": "Paginated measurement results with summary."},
        404: {"model": ErrorResponse, "description": "File not found."},
    },
    summary="Get feature measurements",
    description=(
        "Return paginated measurement results for every feature in the file.  "
        "\n\n"
        "The **summary** block aggregates totals across *all* features "
        "(not just the current page).  "
        "\n\n"
        "Set `include_geometry=true` to include the GeoJSON geometry in each "
        "item; omit or set to `false` for a compact response."
    ),
)
async def get_measurements(
    file_id: str,
    limit: LimitQ = 50,
    offset: OffsetQ = 0,
    include_geometry: Annotated[
        bool,
        Query(description="Include GeoJSON geometry in each feature item."),
    ] = False,
    db: AsyncSession = Depends(get_db),
) -> MeasurementsResponse:
    """Return paginated measurement results for every feature in the file."""
    file_repo = FileRepository(db)
    feat_repo = FeatureRepository(db)

    # 404 if unknown file
    await file_repo.get_or_404(file_id)

    # Paginated page
    total, page = await feat_repo.paginate_for_file(file_id, limit=limit, offset=offset)

    # Summary requires all features (avoid loading geometry for this)
    all_features = await feat_repo.list_for_file(file_id)
    summary = build_summary(all_features)

    items = [feature_to_measurement_item(f, include_geometry=include_geometry) for f in page]

    return MeasurementsResponse(
        file_id=file_id,
        total=total,
        limit=limit,
        offset=offset,
        summary=summary,
        items=items,
    )


# ---------------------------------------------------------------------------
# GET /files/  (list)
# ---------------------------------------------------------------------------


@router.get(
    "/",
    response_model=list[FileResponse],
    summary="List uploaded files",
    description="Return the most recently uploaded files, newest first.",
)
async def list_files(
    limit: LimitQ = 100,
    offset: OffsetQ = 0,
    db: AsyncSession = Depends(get_db),
) -> list[FileResponse]:
    """Return the most recently uploaded files, newest first."""
    repo = FileRepository(db)
    files = await repo.list_all(limit=limit, offset=offset)
    return [FileResponse.model_validate(f) for f in files]


# ---------------------------------------------------------------------------
# GET /files/{file_id}/features/  (legacy raw-row endpoint)
# ---------------------------------------------------------------------------


@router.get(
    "/{file_id}/features/",
    response_model=list[FeatureResponse],
    summary="List raw feature rows",
    description=(
        "Low-level endpoint that returns ORM Feature rows with JSON-string "
        "fields.  Prefer `/measurements/` for human-readable results."
    ),
    include_in_schema=True,
)
async def list_features(
    file_id: str,
    limit: LimitQ = 50,
    offset: OffsetQ = 0,
    db: AsyncSession = Depends(get_db),
) -> list[FeatureResponse]:
    """Return raw ORM Feature rows with JSON-string fields."""
    file_repo = FileRepository(db)
    await file_repo.get_or_404(file_id)

    feat_repo = FeatureRepository(db)
    _, features = await feat_repo.paginate_for_file(file_id, limit=limit, offset=offset)
    return [FeatureResponse.model_validate(f) for f in features]
