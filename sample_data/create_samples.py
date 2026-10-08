"""Generate realistic sample data files for testing."""

from pathlib import Path
import shutil
import zipfile

import geopandas as gpd
from shapely.geometry import Polygon

sample_dir = Path("sample_data")
sample_dir.mkdir(exist_ok=True)

# 1. KML Sample
kml_content = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>Sample Parcels and Roads</name>
    <Folder>
      <name>Parcels</name>
      <Placemark>
        <name>Parcel Alpha</name>
        <description>Agricultural zone</description>
        <Polygon>
          <outerBoundaryIs>
            <LinearRing>
              <coordinates>
                9.0,51.0,0 9.01,51.0,0 9.01,51.01,0 9.0,51.01,0 9.0,51.0,0
              </coordinates>
            </LinearRing>
          </outerBoundaryIs>
        </Polygon>
      </Placemark>
      <Placemark>
        <name>Parcel Beta</name>
        <description>Commercial zone</description>
        <Polygon>
          <outerBoundaryIs>
            <LinearRing>
              <coordinates>
                9.02,51.0,0 9.03,51.0,0 9.03,51.01,0 9.02,51.01,0 9.02,51.0,0
              </coordinates>
            </LinearRing>
          </outerBoundaryIs>
        </Polygon>
      </Placemark>
    </Folder>
    <Folder>
      <name>Transport</name>
      <Placemark>
        <name>Main Highway</name>
        <description>State connector</description>
        <LineString>
          <coordinates>
            9.0,51.0,0 9.015,51.005,0 9.03,51.01,0
          </coordinates>
        </LineString>
      </Placemark>
    </Folder>
  </Document>
</kml>
"""
(sample_dir / "parcels.kml").write_text(kml_content.strip(), encoding="utf-8")

# 2. Shapefile ZIP Sample
poly1 = Polygon([(9.00, 51.00), (9.01, 51.00), (9.01, 51.01), (9.00, 51.01), (9.00, 51.00)])
poly2 = Polygon([(9.02, 51.00), (9.03, 51.00), (9.03, 51.01), (9.02, 51.01), (9.02, 51.00)])
gdf = gpd.GeoDataFrame(
    {
        "id": [101, 102],
        "name": ["North Lot", "South Lot"],
        "zone": ["R-1", "C-2"],
    },
    geometry=[poly1, poly2],
    crs="EPSG:4326",
)

staging_dir = sample_dir / "shp_staging"
staging_dir.mkdir(exist_ok=True)
shp_path = staging_dir / "sample_parcels.shp"
gdf.to_file(str(shp_path), engine="pyogrio")

zip_path = sample_dir / "parcels.zip"
with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        f = staging_dir / f"sample_parcels{ext}"
        if f.exists():
            zf.write(f, arcname=f.name)

shutil.rmtree(staging_dir, ignore_errors=True)
print("Successfully generated sample_data/parcels.kml and sample_data/parcels.zip!")
