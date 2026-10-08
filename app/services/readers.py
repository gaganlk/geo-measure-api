"""readers.py — read a zipped Shapefile or KML into a common feature list.

Public API
----------
FeatureRecord   – typed dataclass returned by both readers
read_shapefile  – accepts the path to the extracted .shp file
read_kml        – accepts the path to the .kml file
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import json
import logging
import math
from pathlib import Path
from typing import Any

import numpy as np
import pyogrio
import shapely
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Common return type
# ---------------------------------------------------------------------------


@dataclass
class FeatureRecord:
    """A single feature extracted from any supported file format."""

    index: int  # stable, unique across all layers in the file
    geometry: BaseGeometry | None  # Shapely geometry (2-D), or None
    geometry_type: str | None  # e.g. "Polygon", "LineString", None
    properties: dict[str, Any]  # JSON-safe property dict
    source_crs: str  # EPSG string, e.g. "EPSG:4326"
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    """Recursively convert a value to a JSON-serialisable type.

    Handles numpy scalars, NaN/Inf floats, date/datetime, and fallback str().
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        v = float(value)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [_json_safe(i) for i in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, int | str):
        return value
    # Catch-all: numpy arrays, bytes, etc.
    try:
        return str(value)
    except Exception:
        return None


def _safe_props(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return a JSON-safe copy of a properties dict."""
    if not raw:
        return {}
    return {k: _json_safe(v) for k, v in raw.items()}


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _drop_z(geom: BaseGeometry | None) -> BaseGeometry | None:
    """Return a 2-D version of *geom* (strips Z coordinate if present).

    Uses ``shapely.force_2d()`` which is the correct Shapely 2 API for this.
    The previous GeoJSON round-trip (shape(mapping(geom))) preserved Z in
    Shapely 2 and is no longer a reliable approach.
    """
    if geom is None or geom.is_empty:
        return geom
    if geom.has_z:
        return shapely.force_2d(geom)
    return geom


def _geometry_from_raw(
    raw: Any,
    index: int,
    warnings: list[str],
) -> tuple[BaseGeometry | None, str | None]:
    """Convert a raw geometry object to a Shapely geometry.

    Returns (geometry, geometry_type).  Null / empty geometries are allowed —
    a warning is appended instead of raising.
    """
    if raw is None:
        warnings.append(f"Feature {index}: null geometry.")
        return None, None

    try:
        geom = shape(raw) if isinstance(raw, dict) else raw
    except Exception as exc:
        warnings.append(f"Feature {index}: could not parse geometry — {exc}.")
        return None, None

    if geom is None or geom.is_empty:
        warnings.append(f"Feature {index}: empty geometry.")
        return geom, geom.geom_type if geom is not None else None

    return geom, geom.geom_type


# ---------------------------------------------------------------------------
# Shapefile reader
# ---------------------------------------------------------------------------


def read_shapefile(shp_path: Path) -> list[FeatureRecord]:
    """Read a Shapefile (.shp already extracted) into a list of FeatureRecord.

    Args:
        shp_path: Absolute path to the .shp file inside the extracted temp dir.

    Returns:
        List of FeatureRecord, one per row (including null-geometry rows).
    """
    import geopandas as gpd

    log.debug("Reading shapefile: %s", shp_path)
    gdf = gpd.read_file(shp_path, engine="pyogrio")

    # Determine source CRS
    if gdf.crs is not None:
        source_crs = gdf.crs.to_epsg()
        crs_str = f"EPSG:{source_crs}" if source_crs else gdf.crs.to_string()
    else:
        # .prj validation happens upstream (ingestion); this is defensive
        crs_str = "UNKNOWN"

    records: list[FeatureRecord] = []
    for idx, row in enumerate(gdf.itertuples(index=False)):
        feat_warnings: list[str] = []
        geom_raw = getattr(row, "geometry", None)

        geom, geom_type = _geometry_from_raw(geom_raw, idx, feat_warnings)
        # Drop Z for uniformity
        geom = _drop_z(geom)
        if geom is not None and not geom.is_empty:
            geom_type = geom.geom_type

        # Build properties — everything except the geometry column
        props: dict[str, Any] = {}
        for col in gdf.columns:
            if col == "geometry":
                continue
            props[col] = _json_safe(getattr(row, col, None))

        records.append(
            FeatureRecord(
                index=idx,
                geometry=geom,
                geometry_type=geom_type,
                properties=props,
                source_crs=crs_str,
                warnings=feat_warnings,
            )
        )

    log.debug("Shapefile yielded %d features.", len(records))
    return records


# ---------------------------------------------------------------------------
# KML reader
# ---------------------------------------------------------------------------

_KML_CRS = "EPSG:4326"


def read_kml(kml_path: Path) -> list[FeatureRecord]:
    """Read all layers (folders) of a KML into a list of FeatureRecord.

    - CRS is always EPSG:4326 (KML standard).
    - Z coordinates are dropped.
    - Placemark name/description are kept as properties.
    - feature_index is globally unique across all layers.

    Args:
        kml_path: Absolute path to the .kml file.

    Returns:
        List of FeatureRecord, globally indexed.
    """
    log.debug("Reading KML: %s", kml_path)

    try:
        layer_names: list[str] = pyogrio.list_layers(str(kml_path))[:, 0].tolist()
    except Exception as exc:
        log.warning("pyogrio.list_layers failed: %s. Falling back to default layer.", exc)
        layer_names = [""]

    records: list[FeatureRecord] = []
    global_index = 0

    for layer in layer_names:
        log.debug("KML layer: %r", layer)
        try:
            info = pyogrio.read_info(str(kml_path), layer=layer or None)
            geometry_type_raw = info.get("geometry_type", "Unknown")
            log.debug("Layer geometry type: %s", geometry_type_raw)
        except Exception as exc:
            log.debug("Could not read geometry type for KML layer %r: %s", layer, exc)

        try:
            layer_data = pyogrio.read_dataframe(
                str(kml_path),
                layer=layer or None,
                use_arrow=False,
            )
        except Exception as exc:
            log.warning("Skipping KML layer %r: %s", layer, exc)
            continue

        # pyogrio returns a GeoDataFrame — iterate rows
        for row in layer_data.itertuples(index=False):
            feat_warnings: list[str] = []
            geom_raw = getattr(row, "geometry", None)

            geom, geom_type = _geometry_from_raw(geom_raw, global_index, feat_warnings)
            geom = _drop_z(geom)
            if geom is not None and not geom.is_empty:
                geom_type = geom.geom_type

            # Collect properties; keep Name/Description from KML
            props: dict[str, Any] = {}
            for col in layer_data.columns:
                if col == "geometry":
                    continue
                val = getattr(row, col, None)
                props[col] = _json_safe(val)

            # Ensure layer name is captured when available
            if layer and "layer" not in props:
                props["layer"] = layer

            records.append(
                FeatureRecord(
                    index=global_index,
                    geometry=geom,
                    geometry_type=geom_type,
                    properties=props,
                    source_crs=_KML_CRS,
                    warnings=feat_warnings,
                )
            )
            global_index += 1

    log.debug("KML yielded %d features across %d layers.", len(records), len(layer_names))
    return records


# ---------------------------------------------------------------------------
# Geometry → GeoJSON text helper (used by ingestion)
# ---------------------------------------------------------------------------


def geometry_to_geojson(geom: BaseGeometry | None) -> str | None:
    """Serialise a Shapely geometry to a GeoJSON string, or return None."""
    if geom is None or geom.is_empty:
        return None
    try:
        return json.dumps(mapping(geom))
    except Exception as exc:
        log.warning("Could not serialise geometry to GeoJSON: %s", exc)
        return None
