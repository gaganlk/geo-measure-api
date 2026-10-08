"""conftest_integration.py — shared fixtures for full-stack integration tests.

Provides:
  sync_client     – starlette TestClient wired to a fresh on-disk SQLite DB
                    (per-test isolation via temp dir).
  shapefile_zip   – factory that creates a valid shapefile ZIP from a GeoDataFrame.
  kml_bytes       – factory that renders a KML string template.

Why sync TestClient instead of httpx AsyncClient?
  The upload route uses asyncio.to_thread for CPU work; TestClient's built-in
  event loop handles that transparently and avoids nested-loop problems.
"""

from __future__ import annotations

from collections.abc import Generator
import io
from pathlib import Path
import zipfile

from fastapi.testclient import TestClient
import geopandas as gpd
import pytest
from shapely.geometry import (
    MultiPolygon,
    Polygon,
)

from app.config import get_settings

# ---------------------------------------------------------------------------
# Per-test isolated environment (temp dir for uploads + fresh SQLite file)
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch Settings so each test gets its own upload dir and DB file."""
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    db_path = tmp_path / "test.db"

    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    # Clear the lru_cache so the new env vars are picked up
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture()
def sync_client(isolated_env: Path) -> Generator[TestClient, None, None]:
    """Return a Starlette TestClient backed by an isolated per-test DB."""
    # Import *after* env is patched so the app sees the right settings
    import asyncio

    # Re-create engine and tables for each test's DB
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.database import Base
    from app.main import create_app

    settings = get_settings()

    async def _init_db() -> None:
        eng = create_async_engine(settings.DATABASE_URL)
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await eng.dispose()

    asyncio.run(_init_db())

    app = create_app()
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client


# ---------------------------------------------------------------------------
# Shapefile ZIP factory
# ---------------------------------------------------------------------------


def make_shapefile_zip(
    gdf: gpd.GeoDataFrame,
    tmp_path: Path,
    name: str = "layer",
    include_prj: bool = True,
    include_dbf: bool = True,
    include_shx: bool = True,
) -> bytes:
    """Write *gdf* to a temporary shapefile, zip it, and return the ZIP bytes.

    Args:
        gdf:          GeoDataFrame to write (must have a CRS set).
        tmp_path:     Scratch directory for intermediate files.
        name:         Stem name for the shapefile components.
        include_prj:  Whether to include the .prj file in the ZIP.
        include_dbf:  Whether to include the .dbf file in the ZIP.
        include_shx:  Whether to include the .shx file in the ZIP.
    """
    shp_dir = tmp_path / "shp_staging"
    shp_dir.mkdir(exist_ok=True)
    shp_path = shp_dir / f"{name}.shp"
    gdf.to_file(str(shp_path), engine="pyogrio")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            component = shp_dir / f"{name}{ext}"
            if not component.exists():
                continue
            if ext == ".prj" and not include_prj:
                continue
            if ext == ".dbf" and not include_dbf:
                continue
            if ext == ".shx" and not include_shx:
                continue
            zf.write(component, arcname=f"{name}{ext}")

    return buf.getvalue()


# ---------------------------------------------------------------------------
# KML string factory
# ---------------------------------------------------------------------------


def make_kml(placemarks: list[dict]) -> bytes:
    """Render a minimal KML with the given placemarks.

    Each dict in *placemarks* must have:
      ``name``     – Placemark name
      ``folder``   – Folder name (each unique value becomes a separate Folder)
      ``coords``   – coordinate string for <coordinates>, e.g. "9.0,51.0,0"

    A point placemark looks like: {"name": "P1", "folder": "A", "coords": "9.0,51.0,0"}
    """
    folders: dict[str, list[dict]] = {}
    for pm in placemarks:
        folders.setdefault(pm["folder"], []).append(pm)

    folder_xml = ""
    for folder_name, pms in folders.items():
        pm_xml = ""
        for pm in pms:
            pm_xml += (
                f"<Placemark>"
                f"<name>{pm['name']}</name>"
                f"<Point><coordinates>{pm['coords']}</coordinates></Point>"
                f"</Placemark>"
            )
        folder_xml += f"<Folder><name>{folder_name}</name>{pm_xml}</Folder>"

    kml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2">\n'
        "  <Document>\n"
        "    <name>Test KML</name>\n"
        f"    {folder_xml}\n"
        "  </Document>\n"
        "</kml>\n"
    )
    return kml.encode("utf-8")


def make_polygon_kml(polygons: list[tuple[str, str, list[tuple[float, float]]]]) -> bytes:
    """Render a KML with Polygon placemarks.

    Each tuple: (folder_name, placemark_name, ring_coords as [(lon,lat), ...])
    """
    folders: dict[str, list[tuple[str, list[tuple[float, float]]]]] = {}
    for folder, name, coords in polygons:
        folders.setdefault(folder, []).append((name, coords))

    folder_xml = ""
    for folder_name, pms in folders.items():
        pm_xml = ""
        for name, coords in pms:
            coord_str = " ".join(f"{lon},{lat},0" for lon, lat in coords)
            pm_xml += (
                f"<Placemark>"
                f"<name>{name}</name>"
                f"<Polygon><outerBoundaryIs><LinearRing>"
                f"<coordinates>{coord_str}</coordinates>"
                f"</LinearRing></outerBoundaryIs></Polygon>"
                f"</Placemark>"
            )
        folder_xml += f"<Folder><name>{folder_name}</name>{pm_xml}</Folder>"

    kml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2">\n'
        f"  <Document>{folder_xml}</Document>\n"
        "</kml>\n"
    )
    return kml.encode("utf-8")


def make_geometry_collection_kml() -> bytes:
    """Render a KML with a MultiGeometry (GeometryCollection) placemark."""
    kml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2">\n'
        "  <Document>\n"
        "    <Placemark>\n"
        "      <name>Mixed Collection</name>\n"
        "      <MultiGeometry>\n"
        "        <Point><coordinates>9.0,51.0,0</coordinates></Point>\n"
        "        <LineString><coordinates>9.0,51.0,0 9.1,51.1,0</coordinates></LineString>\n"
        "      </MultiGeometry>\n"
        "    </Placemark>\n"
        "  </Document>\n"
        "</kml>\n"
    )
    return kml.encode("utf-8")


# ---------------------------------------------------------------------------
# Geometry constants (WGS-84, small shapes for accuracy testing)
# ---------------------------------------------------------------------------

# ~1 km × 1 km square near Stuttgart (zone 32N)
STUTTGART_POLY = Polygon(
    [
        (9.000, 51.000),
        (9.010, 51.000),
        (9.010, 51.009),
        (9.000, 51.009),
    ]
)

# Southern hemisphere — São Paulo area (zone 23S)
SAO_PAULO_POLY = Polygon(
    [
        (-46.650, -23.550),
        (-46.640, -23.550),
        (-46.640, -23.540),
        (-46.650, -23.540),
    ]
)

# Near zone-32/33 boundary (12° E) — spans it deliberately
ZONE_BOUNDARY_POLY = Polygon(
    [
        (11.990, 51.000),
        (12.010, 51.000),
        (12.010, 51.001),
        (11.990, 51.001),
    ]
)

# MultiPolygon with a hole in the second member
MULTIPOLY_WITH_HOLE = MultiPolygon(
    [
        (
            [(9.0, 51.0), (9.1, 51.0), (9.1, 51.1), (9.0, 51.1), (9.0, 51.0)],
            [],  # no holes
        ),
        (
            [(9.2, 51.0), (9.3, 51.0), (9.3, 51.1), (9.2, 51.1), (9.2, 51.0)],
            [[(9.22, 51.02), (9.28, 51.02), (9.28, 51.08), (9.22, 51.08), (9.22, 51.02)]],
        ),
    ]
)

# Self-intersecting bowtie
BOWTIE_POLY = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
