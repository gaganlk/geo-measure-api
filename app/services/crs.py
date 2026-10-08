"""crs.py — per-feature CRS detection and best-fit projected CRS selection.

Design
------
Selection is **per-feature**, not per-file, because a single file can span
multiple UTM zones (e.g. a road network crossing a zone boundary or a global
dataset).

Algorithm
~~~~~~~~~
1. If the feature's source CRS is not EPSG:4326, reproject the geometry to
   EPSG:4326 using a cached pyproj Transformer (always_xy=True).
2. Compute the geometry's representative point (guaranteed inside the polygon,
   faster than centroid for large shapes) in WGS-84 lon/lat.
3. Select the UTM zone:
   - |lat| ≤ 84°  → standard UTM zone
       North:  EPSG:326xx  (xx = 1..60)
       South:  EPSG:327xx
   - |lat| > 84°  → polar UPS
       Arctic: EPSG:32661
       Antarctic: EPSG:32761
4. Optionally detect zone-boundary crossing for Polygon/MultiPolygon and
   append a warning when the bounding-box lon range spans a 6° UTM boundary.

Transformer cache
~~~~~~~~~~~~~~~~~
pyproj.Transformer objects are reused per (source_crs, target_crs) pair via a
module-level dict so they are created once per process, not once per feature.

Public API
----------
utm_crs_for_geometry(geometry, source_crs) -> (epsg_str, warnings)
reproject(geometry, source_crs, target_crs)  -> projected Shapely geometry
geodesic_area_m2(geometry)  -> float   (via pyproj.Geod, for cross-checks)
geodesic_length_m(geometry) -> float   (via pyproj.Geod, for cross-checks)
"""

from __future__ import annotations

import logging

import pyproj
from pyproj import Transformer
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Transformer cache  (module-level, process-lifetime)
# ---------------------------------------------------------------------------

_transformer_cache: dict[tuple[str, str], Transformer] = {}


def _get_transformer(source_crs: str, target_crs: str) -> Transformer:
    """Return a cached Transformer for the (source, target) CRS pair."""
    key = (source_crs, target_crs)
    if key not in _transformer_cache:
        _transformer_cache[key] = Transformer.from_crs(
            source_crs,
            target_crs,
            always_xy=True,
        )
    return _transformer_cache[key]


# ---------------------------------------------------------------------------
# Reprojection
# ---------------------------------------------------------------------------


def reproject(
    geometry: BaseGeometry,
    source_crs: str,
    target_crs: str,
) -> BaseGeometry:
    """Reproject *geometry* from *source_crs* to *target_crs*.

    Args:
        geometry:   A 2-D Shapely geometry.
        source_crs: EPSG string or PROJ string of the input CRS.
        target_crs: EPSG string or PROJ string of the output CRS.

    Returns:
        Reprojected Shapely geometry.

    Raises:
        pyproj.exceptions.CRSError: If either CRS string is invalid.
        Exception: Propagated from pyproj if coordinates cannot be transformed.
    """
    if source_crs == target_crs:
        return geometry
    t = _get_transformer(source_crs, target_crs)
    return transform(t.transform, geometry)


# ---------------------------------------------------------------------------
# UTM / UPS zone selection
# ---------------------------------------------------------------------------

_WGS84 = "EPSG:4326"

# Polar thresholds (UTM is undefined above ±84°)
_POLAR_LAT = 84.0
_UPS_ARCTIC = "EPSG:32661"
_UPS_ANTARCTIC = "EPSG:32761"

# UTM zone-boundary width in degrees longitude
_UTM_ZONE_WIDTH = 6.0


def _utm_epsg_from_lonlat(lon: float, lat: float) -> str:
    """Return the EPSG code string for the UTM/UPS zone containing (lon, lat).

    Args:
        lon: Longitude in decimal degrees (−180 … 180).
        lat: Latitude in decimal degrees (−90 … 90).

    Returns:
        EPSG code string, e.g. ``"EPSG:32632"``.
    """
    abs_lat = abs(lat)

    # Polar UPS fallback
    if abs_lat > _POLAR_LAT:
        return _UPS_ARCTIC if lat > 0 else _UPS_ANTARCTIC

    # Standard UTM zone (1..60)
    # Normalise longitude to [0, 360) then floor-divide by 6
    zone = int((lon + 180.0) / _UTM_ZONE_WIDTH) % 60 + 1

    # Norwegian / Svalbard exceptions (UTM zone 32V / 33X / 35X / 37X)
    # These are uncommon special cases; we skip them to keep the implementation
    # simple and document the omission explicitly.
    # See: https://en.wikipedia.org/wiki/Universal_Transverse_Mercator_coordinate_system

    prefix = 326 if lat >= 0 else 327
    return f"EPSG:{prefix}{zone:02d}"


def _bbox_lon_range(geometry: BaseGeometry) -> tuple[float, float]:
    """Return (min_lon, max_lon) of the geometry bounding box."""
    bounds = geometry.bounds  # (minx, miny, maxx, maxy)
    return bounds[0], bounds[2]


def _spans_zone_boundary(min_lon: float, max_lon: float) -> bool:
    """Return True if the longitude span crosses at least one 6° UTM boundary."""
    lo_zone = int((min_lon + 180.0) / _UTM_ZONE_WIDTH)
    hi_zone = int((max_lon + 180.0) / _UTM_ZONE_WIDTH)
    return hi_zone > lo_zone


# ---------------------------------------------------------------------------
# Public: per-feature CRS selection
# ---------------------------------------------------------------------------


def utm_crs_for_geometry(
    geometry: BaseGeometry,
    source_crs: str,
) -> tuple[str, list[str]]:
    """Select the best projected CRS for *geometry* and return it with warnings.

    The geometry is briefly reprojected to EPSG:4326 (if not already there) to
    compute its representative point, which determines the UTM/UPS zone.

    Args:
        geometry:   Shapely geometry (2-D, not empty).
        source_crs: CRS of *geometry* as an EPSG/PROJ string.

    Returns:
        ``(epsg_str, warnings)`` — the chosen projected EPSG code and a list
        of non-fatal warning strings (e.g. zone-boundary crossing).

    Raises:
        Exception: Propagated from pyproj if reprojection fails.
    """
    warnings: list[str] = []

    # Step 1 — ensure we have WGS-84 coordinates for zone selection
    if source_crs.upper() == _WGS84:
        geom_wgs84 = geometry
    else:
        geom_wgs84 = reproject(geometry, source_crs, _WGS84)

    # Step 2 — representative point (always inside the geometry, unlike centroid)
    rep = geom_wgs84.representative_point()
    lon, lat = rep.x, rep.y

    # Step 3 — select zone
    projected_crs = _utm_epsg_from_lonlat(lon, lat)

    # Step 4 — zone-boundary warning for area/line features
    if not geom_wgs84.is_empty:
        min_lon, max_lon = _bbox_lon_range(geom_wgs84)
        if _spans_zone_boundary(min_lon, max_lon):
            lo_zone = int((min_lon + 180.0) / _UTM_ZONE_WIDTH) % 60 + 1
            hi_zone = int((max_lon + 180.0) / _UTM_ZONE_WIDTH) % 60 + 1
            warnings.append(
                f"Geometry spans UTM zone boundary "
                f"(zones {lo_zone}–{hi_zone}); "
                f"measurements projected into zone {projected_crs} "
                f"from representative point ({lon:.4f}°, {lat:.4f}°). "
                "Consider splitting the feature for higher accuracy."
            )

    return projected_crs, warnings


# ---------------------------------------------------------------------------
# Geodesic cross-check helpers (pyproj.Geod)
# ---------------------------------------------------------------------------

_geod = pyproj.Geod(ellps="WGS84")


def geodesic_area_m2(geometry: BaseGeometry) -> float:
    """Compute the geodesic area of *geometry* in square metres.

    Uses pyproj.Geod.geometry_area_perimeter() on WGS-84 coordinates.
    The geometry **must already be in EPSG:4326** (lon/lat degrees).

    Returns the absolute area (sign indicates winding order; we discard it).

    Raises:
        ValueError: If *geometry* is not a Polygon / MultiPolygon.
    """
    area, _ = _geod.geometry_area_perimeter(geometry)
    return abs(area)


def geodesic_length_m(geometry: BaseGeometry) -> float:
    """Compute the geodesic length of *geometry* in metres.

    Uses pyproj.Geod.geometry_length() on WGS-84 coordinates.
    The geometry **must already be in EPSG:4326** (lon/lat degrees).

    Returns:
        Total length in metres across all sub-geometries.
    """
    return abs(_geod.geometry_length(geometry))
