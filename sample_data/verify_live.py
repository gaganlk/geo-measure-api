"""Live verification script for running geo-measure-api server."""

import json
from pathlib import Path

import httpx

client = httpx.Client(base_url="http://127.0.0.1:8000", timeout=10.0)

print("=== 1. HEALTH CHECK ===")
r_health = client.get("/health")
print("Status:", r_health.status_code, "Body:", r_health.json())
assert r_health.status_code == 200
assert r_health.json()["status"] == "ok"

print("\n=== 2. OPENAPI DOCS ===")
r_docs = client.get("/docs")
print("Docs status:", r_docs.status_code)
assert r_docs.status_code == 200

print("\n=== 3. UPLOAD KML (sample_data/parcels.kml) ===")
kml_bytes = Path("sample_data/parcels.kml").read_bytes()
r_kml = client.post(
    "/api/files/",
    files={"file": ("parcels.kml", kml_bytes, "application/vnd.google-earth.kml+xml")},
)
print("Upload status:", r_kml.status_code)
kml_info = r_kml.json()
print("Response:", json.dumps(kml_info, indent=2))
assert r_kml.status_code == 201
assert kml_info["status"] == "COMPLETED"
assert kml_info["feature_count"] == 3
kml_id = kml_info["id"]

print("\n=== 4. UPLOAD SHAPEFILE ZIP (sample_data/parcels.zip) ===")
zip_bytes = Path("sample_data/parcels.zip").read_bytes()
r_zip = client.post("/api/files/", files={"file": ("parcels.zip", zip_bytes, "application/zip")})
print("Upload status:", r_zip.status_code)
zip_info = r_zip.json()
print("Response:", json.dumps(zip_info, indent=2))
assert r_zip.status_code == 201
assert zip_info["status"] == "COMPLETED"
assert zip_info["feature_count"] == 2
shp_id = zip_info["id"]

print("\n=== 5. LIST ALL FILES ===")
r_list = client.get("/api/files/")
print("List status:", r_list.status_code)
files = r_list.json()
print(f"Total uploaded files found: {len(files)}")
assert len(files) >= 2

print("\n=== 6. GET FILE METADATA ===")
r_get = client.get(f"/api/files/{shp_id}/")
print("Get file status:", r_get.status_code)
print("Metadata:", json.dumps(r_get.json(), indent=2))
assert r_get.status_code == 200
assert r_get.json()["id"] == shp_id

print("\n=== 7. GET MEASUREMENTS FOR SHAPEFILE ===")
r_meas = client.get(f"/api/files/{shp_id}/measurements/?include_geometry=true")
print("Measurements status:", r_meas.status_code)
meas_data = r_meas.json()
print("Summary:", json.dumps(meas_data["summary"], indent=2))
print("First item measurements:")
print(json.dumps(meas_data["items"][0]["measurements"], indent=2))
print("Projected CRS:", meas_data["items"][0]["projected_crs"])
assert r_meas.status_code == 200
assert meas_data["summary"]["total_area_m2"] > 0
assert meas_data["items"][0]["measurements"]["area_m2"] > 0
assert meas_data["items"][0]["measurements"]["area_ha"] > 0
assert meas_data["items"][0]["measurements"]["perimeter_m"] > 0

print("\n=== 8. GET MEASUREMENTS FOR KML (POLYGONS + LINESTRING) ===")
r_kml_meas = client.get(f"/api/files/{kml_id}/measurements/?include_geometry=true")
kml_meas_data = r_kml_meas.json()
print("Summary:", json.dumps(kml_meas_data["summary"], indent=2))
for item in kml_meas_data["items"]:
    print(
        f"- Feature {item['feature_index']} ({item['geometry_type']}): status={item['measurement_status']}, projected={item['projected_crs']}"
    )
    if item["geometry_type"] == "LineString":
        print(
            f"  Length: {item['measurements']['length_m']:.2f} m ({item['measurements']['length_km']:.4f} km)"
        )
    elif item["geometry_type"] == "Polygon":
        print(
            f"  Area: {item['measurements']['area_m2']:.2f} m2 ({item['measurements']['area_ha']:.4f} ha), Perimeter: {item['measurements']['perimeter_m']:.2f} m"
        )

print("\n=== 9. ERROR TEST: 404 NOT FOUND ===")
r_404 = client.get("/api/files/nonexistent_file_id/")
print("Status:", r_404.status_code, "Body:", r_404.json())
assert r_404.status_code == 404
assert r_404.json()["error"]["code"] == "NOT_FOUND"

print("\n=== 10. ERROR TEST: 415 UNSUPPORTED FILE TYPE ===")
r_415 = client.post("/api/files/", files={"file": ("notes.txt", b"Hello", "text/plain")})
print("Status:", r_415.status_code, "Body:", r_415.json())
assert r_415.status_code == 415
assert r_415.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"

print("\nALL LIVE ENDPOINT CHECKS PASSED PERFECTLY!")
