"""Tests for app/services/ingestion.py — validation, zip security, and KML checks.

These tests are pure-Python and do not require GeoPandas/GDAL to be installed;
they target only the validation/extraction logic.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import AsyncMock, patch
import zipfile

import pytest
from shapely.geometry import Polygon

from app.core.errors import (
    FileTooLargeError,
    UnsupportedFileTypeError,
    ValidationError,
    ZipSecurityError,
)
from app.db.models import File, FileStatus
from app.services.ingestion import (
    _check_kml_xml,
    _find_shp,
    _safe_extract_zip,
    _validate_shapefile_companions,
    ingest_file,
    validate_upload,
)
from app.services.readers import FeatureRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_zip(entries: dict[str, bytes], dest: Path) -> Path:
    """Write a ZIP file at *dest* containing *entries* {name: content}."""
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return dest


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# validate_upload
# ---------------------------------------------------------------------------


class TestValidateUpload:
    def test_zip_accepted(self) -> None:
        data = _zip_bytes({"x.shp": b""})
        result = validate_upload("test.zip", data[:2], len(data))
        assert result == "shapefile"

    def test_kml_accepted(self) -> None:
        result = validate_upload("test.kml", b"<kml>", 5)
        assert result == "kml"

    def test_unsupported_extension(self) -> None:
        with pytest.raises(UnsupportedFileTypeError):
            validate_upload("data.geojson", b"{}", 2)

    def test_wrong_magic_bytes_for_zip(self) -> None:
        with pytest.raises(ValidationError, match="not a valid ZIP"):
            validate_upload("data.zip", b"XX", 2)

    def test_content_length_too_large(self) -> None:
        """Simulate a Content-Length that exceeds the default 50 MB cap."""
        big = 51 * 1024 * 1024
        with pytest.raises(FileTooLargeError):
            validate_upload("data.zip", b"PK", big)

    def test_kml_binary_garbage_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not appear to be XML"):
            validate_upload("data.kml", b"\x00\x01\x02\x03", 4)

    def test_kml_bom_accepted(self) -> None:
        """UTF-8 BOM (0xEF) prefix is a valid XML start."""
        result = validate_upload("layer.kml", b"\xef\xbb\xbf<kml>", 8)
        assert result == "kml"

    def test_case_insensitive_extension(self) -> None:
        data = _zip_bytes({"x.shp": b""})
        result = validate_upload("SHAPES.ZIP", data[:2], len(data))
        assert result == "shapefile"


# ---------------------------------------------------------------------------
# _check_kml_xml
# ---------------------------------------------------------------------------


class TestCheckKmlXml:
    def test_valid_xml_passes(self, tmp_path: Path) -> None:
        p = tmp_path / "ok.kml"
        p.write_text("<kml><Document/></kml>", encoding="utf-8")
        _check_kml_xml(p)  # must not raise

    def test_broken_xml_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.kml"
        p.write_text("<kml><Document>UNCLOSED", encoding="utf-8")
        with pytest.raises(ValidationError, match="not well-formed XML"):
            _check_kml_xml(p)


# ---------------------------------------------------------------------------
# _safe_extract_zip
# ---------------------------------------------------------------------------


class TestSafeExtractZip:
    def test_clean_zip_extracted(self, tmp_path: Path) -> None:
        src = tmp_path / "clean.zip"
        _make_zip({"layer.shp": b"content", "layer.dbf": b""}, src)
        dest = tmp_path / "out"
        dest.mkdir()
        _safe_extract_zip(src, dest)
        assert (dest / "layer.shp").exists()

    def test_zip_slip_blocked(self, tmp_path: Path) -> None:
        """Entry with ../ in path must be rejected."""
        src = tmp_path / "slip.zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../evil.sh", "rm -rf /")
        src.write_bytes(buf.getvalue())
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(ZipSecurityError, match="path traversal"):
            _safe_extract_zip(src, dest)

    def test_absolute_path_blocked(self, tmp_path: Path) -> None:
        src = tmp_path / "abs.zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            info = zipfile.ZipInfo("/etc/passwd")
            zf.writestr(info, "root:x")
        src.write_bytes(buf.getvalue())
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(ZipSecurityError, match="absolute path"):
            _safe_extract_zip(src, dest)

    def test_too_many_entries_blocked(self, tmp_path: Path) -> None:
        src = tmp_path / "big.zip"
        # default MAX_ZIP_ENTRIES = 50
        entries = {f"file_{i}.txt": b"x" for i in range(51)}
        _make_zip(entries, src)
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(ZipSecurityError, match="entries"):
            _safe_extract_zip(src, dest)

    def test_zip_bomb_blocked(self, tmp_path: Path) -> None:
        """Total uncompressed size > MAX_UNZIPPED_MB (200 MB) must be blocked."""
        src = tmp_path / "bomb.zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
            # Each entry reports 5 MB; 41 × 5 = 205 MB → exceeds 200 MB limit
            big_chunk = b"\x00" * (5 * 1024 * 1024)
            for i in range(41):
                zf.writestr(f"chunk_{i}.bin", big_chunk)
        src.write_bytes(buf.getvalue())
        dest = tmp_path / "out"
        dest.mkdir()
        # entry-count guard fires first (41 < 50, so only bomb guard applies)
        with pytest.raises(ZipSecurityError, match="expand"):
            _safe_extract_zip(src, dest)


# ---------------------------------------------------------------------------
# _find_shp
# ---------------------------------------------------------------------------


class TestFindShp:
    def test_finds_single_shp(self, tmp_path: Path) -> None:
        (tmp_path / "layer.shp").touch()
        result = _find_shp(tmp_path)
        assert result.name == "layer.shp"

    def test_ignores_macosx(self, tmp_path: Path) -> None:
        macos_dir = tmp_path / "__MACOSX"
        macos_dir.mkdir()
        (macos_dir / "._layer.shp").touch()
        (tmp_path / "layer.shp").touch()
        result = _find_shp(tmp_path)
        assert result.name == "layer.shp"

    def test_no_shp_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="No .shp"):
            _find_shp(tmp_path)

    def test_multiple_shp_raises(self, tmp_path: Path) -> None:
        (tmp_path / "a.shp").touch()
        (tmp_path / "b.shp").touch()
        with pytest.raises(ValidationError, match="Multiple"):
            _find_shp(tmp_path)


# ---------------------------------------------------------------------------
# _validate_shapefile_companions
# ---------------------------------------------------------------------------


class TestValidateShapefileCompanions:
    def _touch_all(self, parent: Path, stem: str, exts: list[str]) -> None:
        for ext in exts:
            (parent / f"{stem}{ext}").touch()

    def test_all_present_passes(self, tmp_path: Path) -> None:
        self._touch_all(tmp_path, "layer", [".shp", ".shx", ".dbf", ".prj"])
        _validate_shapefile_companions(tmp_path / "layer.shp")  # must not raise

    def test_missing_shx_raises(self, tmp_path: Path) -> None:
        self._touch_all(tmp_path, "layer", [".shp", ".dbf", ".prj"])
        with pytest.raises(ValidationError, match=".shx"):
            _validate_shapefile_companions(tmp_path / "layer.shp")

    def test_missing_dbf_raises(self, tmp_path: Path) -> None:
        self._touch_all(tmp_path, "layer", [".shp", ".shx", ".prj"])
        with pytest.raises(ValidationError, match=".dbf"):
            _validate_shapefile_companions(tmp_path / "layer.shp")

    def test_missing_prj_raises_crs_message(self, tmp_path: Path) -> None:
        self._touch_all(tmp_path, "layer", [".shp", ".shx", ".dbf"])
        with pytest.raises(ValidationError, match="CRS cannot be determined"):
            _validate_shapefile_companions(tmp_path / "layer.shp")

    def test_case_mixed_companions_pass(self, tmp_path: Path) -> None:
        """Files named PARCEL.SHP, parcel.SHX, Parcel.dbf, pArCeL.PRJ must pass."""
        (tmp_path / "PARCEL.SHP").touch()
        (tmp_path / "parcel.SHX").touch()
        (tmp_path / "Parcel.dbf").touch()
        (tmp_path / "pArCeL.PRJ").touch()
        _validate_shapefile_companions(tmp_path / "PARCEL.SHP")


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------


class TestIngestionEdgeCases:
    def test_empty_zip_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not a valid ZIP archive"):
            validate_upload("test.zip", b"", 0)

    def test_empty_kml_rejected(self) -> None:
        with pytest.raises(ValidationError, match="File is empty"):
            validate_upload("test.kml", b"", 0)

    def test_truncated_zip_raises_validation_error(self, tmp_path: Path) -> None:
        src = tmp_path / "truncated.zip"
        src.write_bytes(b"PK\x03\x04\x14\x00\x00\x00\x08\x00truncated")
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(ValidationError, match="valid ZIP archive"):
            _safe_extract_zip(src, dest)

    def test_zip_with_symlink_rejected(self, tmp_path: Path) -> None:
        src = tmp_path / "symlink.zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            info = zipfile.ZipInfo("symlink_entry")
            # Unix symlink attribute: high byte 0xA (e.g. 0xA1ED)
            info.external_attr = 0xA1ED << 16
            zf.writestr(info, "target_file")
        src.write_bytes(buf.getvalue())
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(ZipSecurityError, match="symbolic link"):
            _safe_extract_zip(src, dest)

    def test_kml_no_placemarks_valid_xml(self, tmp_path: Path) -> None:
        p = tmp_path / "empty_doc.kml"
        p.write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
            <kml xmlns="http://www.opengis.net/kml/2.2">
              <Document>
                <name>Empty Document</name>
              </Document>
            </kml>""",
            encoding="utf-8",
        )
        _check_kml_xml(p)  # Valid XML structure must pass check

    def test_nested_directory_shapefile_found(self, tmp_path: Path) -> None:
        sub = tmp_path / "nested" / "folder"
        sub.mkdir(parents=True)
        (sub / "layer.shp").touch()
        result = _find_shp(tmp_path)
        assert result == sub / "layer.shp"


# ---------------------------------------------------------------------------
# ingest_file orchestrator tests
# ---------------------------------------------------------------------------


class TestIngestFileOrchestrator:
    @pytest.mark.asyncio
    async def test_ingest_shapefile_success(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "test.zip"
        _make_zip(
            {
                "data.shp": b"dummy",
                "data.shx": b"dummy",
                "data.dbf": b"dummy",
                "data.prj": b"dummy",
            },
            zip_path,
        )
        file_rec = File(id="test-file-1", filename="test.zip", file_type="shapefile")
        db = AsyncMock()

        mock_record = FeatureRecord(
            index=0,
            geometry=Polygon([(0, 0), (1, 0), (1, 1), (0, 0)]),
            geometry_type="Polygon",
            properties={"name": "test"},
            source_crs="EPSG:4326",
            warnings=["test warning"],
        )

        with (
            patch("app.services.ingestion.read_shapefile", return_value=[mock_record]),
            patch("app.services.ingestion.FileRepository") as mock_file_repo,
            patch("app.services.ingestion.FeatureRepository") as mock_feat_repo,
        ):
            mock_file_repo_inst = AsyncMock()
            mock_file_repo.return_value = mock_file_repo_inst
            mock_feat_repo_inst = AsyncMock()
            mock_feat_repo.return_value = mock_feat_repo_inst

            await ingest_file(zip_path, file_rec, db)

            mock_feat_repo_inst.bulk_create.assert_awaited_once()
            mock_file_repo_inst.update_status.assert_awaited_once_with(
                file_rec, status=FileStatus.COMPLETED, feature_count=1, crs="EPSG:4326"
            )

    @pytest.mark.asyncio
    async def test_ingest_kml_success(self, tmp_path: Path) -> None:
        kml_path = tmp_path / "test.kml"
        kml_path.write_text("<kml><Document/></kml>", encoding="utf-8")
        file_rec = File(id="test-file-2", filename="test.kml", file_type="kml")
        db = AsyncMock()

        with (
            patch("app.services.ingestion.read_kml", return_value=[]),
            patch("app.services.ingestion.FileRepository") as mock_file_repo,
            patch("app.services.ingestion.FeatureRepository") as mock_feat_repo,
        ):
            mock_file_repo_inst = AsyncMock()
            mock_file_repo.return_value = mock_file_repo_inst
            mock_feat_repo_inst = AsyncMock()
            mock_feat_repo.return_value = mock_feat_repo_inst

            await ingest_file(kml_path, file_rec, db)

            mock_file_repo_inst.update_status.assert_awaited_once_with(
                file_rec, status=FileStatus.COMPLETED, feature_count=0, crs="EPSG:4326"
            )

    @pytest.mark.asyncio
    async def test_ingest_unknown_file_type_raises(self, tmp_path: Path) -> None:
        file_rec = File(id="test-file-3", filename="test.unknown", file_type="geojson")
        db = AsyncMock()

        with (
            patch("app.services.ingestion.FileRepository") as mock_file_repo,
            patch("app.services.ingestion.FeatureRepository"),
        ):
            mock_file_repo_inst = AsyncMock()
            mock_file_repo.return_value = mock_file_repo_inst

            from app.core.errors import ProcessingError

            with pytest.raises(ProcessingError, match="Unknown file_type"):
                await ingest_file(tmp_path / "test.geojson", file_rec, db)

            mock_file_repo_inst.update_status.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ingest_unexpected_exception_handles_gracefully(self, tmp_path: Path) -> None:
        kml_path = tmp_path / "test.kml"
        kml_path.write_text("<kml><Document/></kml>", encoding="utf-8")
        file_rec = File(id="test-file-4", filename="test.kml", file_type="kml")
        db = AsyncMock()

        with (
            patch("app.services.ingestion.read_kml", side_effect=RuntimeError("GDAL crash")),
            patch("app.services.ingestion.FileRepository") as mock_file_repo,
            patch("app.services.ingestion.FeatureRepository"),
        ):
            mock_file_repo_inst = AsyncMock()
            mock_file_repo.return_value = mock_file_repo_inst

            from app.core.errors import ProcessingError

            with pytest.raises(ProcessingError, match="Unexpected error during ingestion"):
                await ingest_file(kml_path, file_rec, db)

            mock_file_repo_inst.update_status.assert_awaited_once()
