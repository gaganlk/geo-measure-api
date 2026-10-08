"""ingestion.py — validate, extract, read, and persist an uploaded geospatial file.

Entry points
------------
validate_upload     – sync, called *before* saving to disk (size + magic bytes)
ingest_file         – async, reads saved file → DB rows

Zip security
------------
Blocks: path traversal (zip-slip), absolute paths, symlinks,
        too many entries (MAX_ZIP_ENTRIES), zip bombs (MAX_UNZIPPED_MB).
Always cleans up the temp extraction directory.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.errors import (
    FileTooLargeError,
    ProcessingError,
    UnsupportedFileTypeError,
    ValidationError,
    ZipSecurityError,
)
from app.db.models import Feature, File, FileStatus, MeasurementStatus
from app.db.repository import FeatureRepository, FileRepository
from app.services.readers import FeatureRecord, geometry_to_geojson, read_kml, read_shapefile

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Magic bytes
# ---------------------------------------------------------------------------

_ZIP_MAGIC = b"PK"  # first 2 bytes of every ZIP


# ---------------------------------------------------------------------------
# Allowed extensions
# ---------------------------------------------------------------------------

_ALLOWED = {".zip", ".kml"}

# Valid first bytes for KML/XML files:
#   0x3C = '<'  (ASCII / UTF-8 — most common)
#   0xEF        = UTF-8 BOM (EF BB BF)
#   0xFE        = UTF-16 BE BOM (FE FF)
#   0xFF        = UTF-16 LE / UTF-32 LE BOM
_KML_FIRST_BYTES: frozenset[int] = frozenset([0x3C, 0xEF, 0xFE, 0xFF])


# ---------------------------------------------------------------------------
# Public: pre-flight validation (call before persisting to disk)
# ---------------------------------------------------------------------------


def validate_upload(filename: str, first_chunk: bytes, content_length: int | None) -> str:
    """Validate a file upload before it is written to disk.

    Args:
        filename:        Original filename supplied by the client.
        first_chunk:     At least the first 2 bytes of the file body.
        content_length:  ``Content-Length`` header value, or None if absent.

    Returns:
        Normalised file type string: ``"shapefile"`` or ``"kml"``.

    Raises:
        UnsupportedFileTypeError: Extension not in {.zip, .kml}.
        FileTooLargeError:        content_length exceeds MAX_UPLOAD_MB.
        ValidationError:          Magic bytes / XML parse mismatch.
    """
    settings = get_settings()
    suffix = Path(filename).suffix.lower()

    if suffix not in _ALLOWED:
        raise UnsupportedFileTypeError(f"Unsupported file type '{suffix}'. Accepted: .zip, .kml")

    if content_length is not None and content_length > settings.max_upload_bytes:
        raise FileTooLargeError(f"File exceeds maximum upload size of {settings.MAX_UPLOAD_MB} MB.")

    if suffix == ".zip":
        if not first_chunk[:2] == _ZIP_MAGIC:
            raise ValidationError("File has a .zip extension but is not a valid ZIP archive.")
        return "shapefile"

    # .kml — we check XML well-formedness later (full content needed), so only
    # do a minimal sanity check here: file must not be binary garbage.
    # UTF-32 BE BOM (00 00 FE FF) starts with 0x00 and is indistinguishable
    # from binary garbage at a single-byte level; we treat it as unsupported.
    stripped = first_chunk.lstrip()
    if stripped and stripped[0] not in _KML_FIRST_BYTES:
        raise ValidationError("File has a .kml extension but does not appear to be XML.")
    return "kml"


# ---------------------------------------------------------------------------
# Internal: safe zip extraction
# ---------------------------------------------------------------------------


def _check_kml_xml(path: Path) -> None:
    """Raise ValidationError if *path* is not well-formed XML."""
    try:
        ET.parse(str(path))
    except ET.ParseError as exc:
        raise ValidationError(f"KML file is not well-formed XML: {exc}") from exc


def _safe_extract_zip(zip_path: Path, dest: Path) -> None:
    """Extract *zip_path* into *dest* with full security checks.

    Checks performed:
    - Entry count ≤ MAX_ZIP_ENTRIES
    - No zip-slip / path traversal (``..`` in name)
    - No absolute paths
    - No symlinks (external attr bit 0xA0000000)
    - Total uncompressed size ≤ MAX_UNZIPPED_MB  (zip-bomb guard)
    """
    settings = get_settings()

    with zipfile.ZipFile(zip_path, "r") as zf:
        entries = zf.infolist()

        # 1. Entry count
        if len(entries) > settings.MAX_ZIP_ENTRIES:
            raise ZipSecurityError(
                f"ZIP contains {len(entries)} entries; maximum is {settings.MAX_ZIP_ENTRIES}."
            )

        # 2. Aggregate uncompressed size (zip-bomb)
        total_uncompressed = sum(e.file_size for e in entries)
        if total_uncompressed > settings.max_unzipped_bytes:
            raise ZipSecurityError(
                f"ZIP would expand to "
                f"{total_uncompressed / (1024 * 1024):.1f} MB; "
                f"maximum is {settings.MAX_UNZIPPED_MB} MB."
            )

        dest_resolved = dest.resolve()

        for entry in entries:
            entry_name = entry.filename
            norm_name = entry_name.replace("\\", "/")
            posix_path = PurePosixPath(norm_name)

            # 3. Absolute paths
            if posix_path.is_absolute() or Path(entry_name).is_absolute():
                raise ZipSecurityError(f"ZIP entry '{entry_name}' has an absolute path.")

            # 4. Path traversal
            if ".." in posix_path.parts or ".." in Path(entry_name).parts:
                raise ZipSecurityError(f"ZIP entry '{entry_name}' contains path traversal.")

            # 5. Symlinks — Unix external_attr high byte == 0xA means symlink
            unix_attr = (entry.external_attr >> 16) & 0xFFFF
            if unix_attr & 0xA000 == 0xA000:
                raise ZipSecurityError(f"ZIP entry '{entry_name}' is a symbolic link.")

            # 6. Validate resolved target stays inside dest
            target = (dest / norm_name).resolve()
            try:
                target.relative_to(dest_resolved)
            except ValueError as err:
                raise ZipSecurityError(
                    f"ZIP entry '{entry_name}' would extract outside destination."
                ) from err

            # Extract single entry
            zf.extract(entry, path=dest)


# ---------------------------------------------------------------------------
# Internal: shapefile discovery & validation
# ---------------------------------------------------------------------------

_MACOSX_PREFIX = "__MACOSX"


def _find_shp(extract_dir: Path) -> Path:
    """Return the path to the .shp file, ignoring __MACOSX artefacts.

    Raises:
        ValidationError: Zero or multiple .shp files found.
    """
    candidates = [
        p
        for p in extract_dir.rglob("*")
        if p.is_file() and p.suffix.lower() == ".shp" and _MACOSX_PREFIX not in p.parts
    ]
    if not candidates:
        raise ValidationError("No .shp file found inside the ZIP archive.")
    if len(candidates) > 1:
        names = ", ".join(str(c.relative_to(extract_dir)) for c in candidates)
        raise ValidationError(
            f"Multiple .shp files found inside the ZIP: {names}. "
            "Please include exactly one shapefile."
        )
    return candidates[0]


def _validate_shapefile_companions(shp_path: Path) -> None:
    """Ensure required sidecar files exist next to *shp_path*.

    Required: .shx, .dbf
    Required for CRS: .prj — raises 422 if missing.
    """
    stem = shp_path.stem.lower()
    parent = shp_path.parent
    files_in_dir = {f.name.lower(): f for f in parent.iterdir() if f.is_file()}

    for ext in (".shx", ".dbf"):
        expected = f"{stem}{ext}".lower()
        if expected not in files_in_dir:
            raise ValidationError(f"Shapefile is missing the required '{ext}' component.")

    expected_prj = f"{stem}.prj".lower()
    if expected_prj not in files_in_dir:
        raise ValidationError(
            "CRS cannot be determined: .prj file is missing from the ZIP archive."
        )


# ---------------------------------------------------------------------------
# Internal: build ORM Feature rows from FeatureRecord list
# ---------------------------------------------------------------------------


def _build_feature_rows(
    records: list[FeatureRecord],
    file_id: str,
) -> list[Feature]:
    rows: list[Feature] = []
    for rec in records:
        geojson_str = geometry_to_geojson(rec.geometry)
        rows.append(
            Feature(
                file_id=file_id,
                feature_index=rec.index,
                geometry_type=rec.geometry_type,
                geometry=geojson_str,
                crs=rec.source_crs,
                properties=json.dumps(rec.properties),
                measurement_status=MeasurementStatus.PENDING,
                measurements=None,
                projected_crs=None,
                warnings=json.dumps(rec.warnings) if rec.warnings else None,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Public: async ingestion orchestrator
# ---------------------------------------------------------------------------


async def ingest_file(
    file_path: Path,
    file_record: File,
    db: AsyncSession,
) -> None:
    """Parse *file_path*, extract features, persist to DB.

    Sequence
    --------
    1. Determine file type from file_record.file_type.
    2. For ZIP: safe-extract → find/validate shapefile → read_shapefile().
       For KML:  validate XML → read_kml().
    3. Build Feature ORM rows and bulk-insert.
    4. Mark file COMPLETED (or FAILED on any error).
    5. Always clean up temp extraction directory.

    The caller (route) controls the final commit.
    """
    file_repo = FileRepository(db)
    feat_repo = FeatureRepository(db)

    extract_dir: Path | None = None

    try:
        records: list[FeatureRecord]

        if file_record.file_type == "shapefile":
            extract_dir = Path(tempfile.mkdtemp(prefix="geo_extract_"))
            _safe_extract_zip(file_path, extract_dir)
            shp_path = _find_shp(extract_dir)
            _validate_shapefile_companions(shp_path)
            records = read_shapefile(shp_path)
            source_crs = records[0].source_crs if records else None

        elif file_record.file_type == "kml":
            _check_kml_xml(file_path)
            records = read_kml(file_path)
            source_crs = records[0].source_crs if records else "EPSG:4326"

        else:
            raise ProcessingError(f"Unknown file_type '{file_record.file_type}'.")

        feature_rows = _build_feature_rows(records, file_record.id)

        if feature_rows:
            await feat_repo.bulk_create(feature_rows)

        await file_repo.update_status(
            file_record,
            status=FileStatus.COMPLETED,
            feature_count=len(records),
            crs=source_crs,
        )
        log.info(
            "Ingested file %s (%s features, CRS=%s).",
            file_record.id,
            len(records),
            source_crs,
        )

    except (
        UnsupportedFileTypeError,
        FileTooLargeError,
        ValidationError,
        ZipSecurityError,
        ProcessingError,
    ) as exc:
        log.warning("Ingestion rejected file %s: %s", file_record.id, exc.message)
        await file_repo.update_status(
            file_record,
            status=FileStatus.FAILED,
            error=exc.message,
        )
        raise

    except Exception as exc:
        msg = f"Unexpected error during ingestion: {exc}"
        log.exception("Ingestion failed for file %s", file_record.id)
        await file_repo.update_status(
            file_record,
            status=FileStatus.FAILED,
            error=msg,
        )
        raise ProcessingError(msg) from exc

    finally:
        if extract_dir is not None and extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)
            log.debug("Cleaned up temp dir: %s", extract_dir)
