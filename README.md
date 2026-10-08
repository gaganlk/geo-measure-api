# geo-measure-api

A production-quality FastAPI service that accepts a zipped Shapefile or a KML file,
extracts every feature, selects a best-fit projected CRS **per feature**, and returns
area and length measurements in SI units.

> 📄 **Evaluation Report:** Complete project writeup and audit documentation are available at [`docs/geo-measure-api-report.pdf`](docs/geo-measure-api-report.pdf).

---

## Table of Contents

1. [Overview & Assignment Compliance](#overview--assignment-compliance)
2. [Setup](#setup)
   - [Local (virtualenv)](#local-virtualenv)
   - [Docker](#docker)
3. [Running tests](#running-tests)
4. [API](#api)
   - [POST /api/files/](#post-apifiles)
   - [GET /api/files/{id}/](#get-apifiles-id)
   - [GET /api/files/{id}/measurements/](#get-apifiles-idmeasurements)
   - [GET /api/files/](#get-apifiles)
   - [Error envelope](#error-envelope)
5. [Architecture](#architecture)
   - [Application structure](#application-structure)
   - [File-processing flow](#file-processing-flow)
   - [Measurement calculation flow](#measurement-calculation-flow)
   - [CRS handling](#crs-handling)
6. [Design decisions](#design-decisions)
7. [Limitations & accuracy](#limitations--accuracy)
8. [Learnings](#learnings)
9. [Future scope](#future-scope)

---

## Overview & Assignment Compliance

### Requirements Compliance Matrix

| Requirement | Spec Item | Implementation Details | Status |
|---|---|---|:---:|
| **1. Backend Framework** | FastAPI or Django+DRF | FastAPI (Python 3.12+), async SQLAlchemy 2, Pydantic v2 | ✅ Complete |
| **2. File Upload** | `POST /api/files/` (.zip Shapefile, .kml) | Streaming validation, Zip-slip & size checks, magic byte detection | ✅ Complete |
| **3. File Processing** | ID, geometry type, geometry, CRS, properties; graceful failure | Isolated per-feature errors; repairs invalid geometries with `make_valid` | ✅ Complete |
| **4. Measurements** | Polygon (Area), LineString (Length), Point (None) | `area_m2`, `area_ha`, `perimeter_m`, `length_m`, `length_km`, `NOT_REQUIRED` for Point | ✅ Complete |
| **5. CRS Handling** | Projected CRS transformation; no degrees | Dynamic per-feature UTM zone selection (with Polar UPS fallback) | ✅ Complete |
| **6. API Design** | `POST /api/files/`, `GET /api/files/{id}/`, `GET /api/files/{id}/measurements/` | Full standard envelopes, OpenAPI docs (`/docs`), pagination & geometry inclusion | ✅ Complete |
| **7. Documentation** | Setup, API, Architecture, Design Decisions in README | Comprehensive guides with copy-paste curl commands & diagrams | ✅ Complete |
| **8. Submission** | Public GitHub repo, Learnings & Future scope | Conventional git commits, CI workflow, report in `docs/` | ✅ Complete |

---

**Accepted formats**

| Extension | Detected as |
|-----------|-------------|
| `.zip` | Zipped Shapefile — must contain exactly one `.shp` with sidecar `.shx`, `.dbf`, and `.prj` |
| `.kml` | KML — all Folders are iterated; feature indices are globally unique across folders |

**What is returned per feature**

| Geometry type | Measurements |
|---|---|
| Polygon / MultiPolygon | `area_m2`, `area_ha`, `perimeter_m` |
| LineString / MultiLineString | `length_m`, `length_km` |
| Point / MultiPoint | `NOT_REQUIRED` — no meaningful metric |
| GeometryCollection | `UNSUPPORTED` — heterogeneous, must be split |
| Invalid geometry | Repaired with `shapely.make_valid`; `INVALID_GEOMETRY` if repair fails |
| Empty / null geometry | `EMPTY` |

One bad feature **never** fails the whole file — errors are isolated per feature.

---

## Setup

### Local (virtualenv)

**Prerequisites:** Python 3.12+ (Docker image pins `python:3.12-slim`; CI runs on `3.12`), GDAL ≥ 3.6, libgeos, libproj (Ubuntu: `sudo apt-get install gdal-bin libgdal-dev libgeos-dev libproj-dev`).

```bash
# 1. Clone and enter the repo
git clone https://github.com/gaganlk/geo-measure-api.git
cd geo-measure-api

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
make install
# equivalent: pip install --upgrade pip && pip install -r requirements.txt

# 4. (Optional) copy the env template and edit values
cp .env.example .env

# 5. Start the development server
make run
# equivalent: uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The API is now reachable at `http://localhost:8000`.  
Interactive docs: `http://localhost:8000/docs`  
ReDoc: `http://localhost:8000/redoc`

---

### Docker

**Prerequisites:** Docker ≥ 24, Docker Compose plugin ≥ 2.

```bash
# Build the multi-stage image (builder + slim runtime)
make docker-build

# Start in the background
make docker-up

# Tail logs
make docker-logs

# Stop
make docker-down
```

The SQLite database and uploaded files are stored in the named volume `geo_measure_data`
which survives `docker compose down`.  To inspect the volume:

```bash
docker volume inspect geo_measure_data
```

Override the host port without editing `docker-compose.yml`:

```bash
HOST_PORT=9000 docker compose up -d
```

---

## Running tests

```bash
# Fast unit tests — no coverage output
make test

# Full suite with branch coverage (terminal + htmlcov/)
make test-cov

# Unit tests only — no GDAL/GeoPandas needed, useful in CI pre-check
make test-unit

# Full-stack integration tests (requires GDAL installed)
make test-integration
```

Coverage reports are written to `htmlcov/` (HTML) and are also emitted as GitHub
Actions artifacts when running in CI.

---

## API

Base URL: `http://localhost:8000`

Interactive Swagger UI is available at `/docs` and ReDoc at `/redoc`. All error responses use the same
[error envelope](#error-envelope).

---

### POST /api/files/

Upload a Shapefile ZIP or KML. Returns `201 Created` with the file record.

**Request**

```
POST /api/files/
Content-Type: multipart/form-data
```

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file | ✅ | `.zip` (Shapefile) or `.kml` |

**Limits**

| Setting | Default | Env var |
|---|---|---|
| Max upload size | 50 MB | `MAX_UPLOAD_MB` |
| Max ZIP uncompressed size | 200 MB | `MAX_UNZIPPED_MB` |
| Max ZIP entries | 50 | `MAX_ZIP_ENTRIES` |

**curl example — Shapefile**

```bash
curl -s -X POST http://localhost:8000/api/files/ \
  -F "file=@parcels.zip" | jq .
```

**Sample response (201)**

```json
{
  "id": "3f2a1b4c5d6e7f8a9b0c1d2e3f4a5b6c",
  "filename": "parcels.zip",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "status": "COMPLETED"
}
```

**curl example — KML**

```bash
curl -s -X POST http://localhost:8000/api/files/ \
  -F "file=@roads.kml" | jq .
```

**Sample response (201)**

```json
{
  "id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
  "filename": "roads.kml",
  "feature_count": 12,
  "crs": "EPSG:4326",
  "status": "COMPLETED"
}
```

**Error responses**

| Status | Code | Reason |
|---|---|---|
| 413 | `FILE_TOO_LARGE` | `Content-Length` header or streamed body exceeds `MAX_UPLOAD_MB` |
| 415 | `UNSUPPORTED_FILE_TYPE` | Extension is not `.zip` or `.kml` |
| 422 | `VALIDATION_ERROR` | ZIP magic bytes wrong; KML is not well-formed XML; missing `.prj`, `.dbf`, or `.shx` |
| 400 | `ZIP_SECURITY_ERROR` | Zip-slip path traversal, symlink, absolute path, or zip-bomb detected |

> **No database record is created for rejected uploads.** A `FAILED` record only
> appears when the file passed all pre-flight checks but failed during reading or
> measuring.

---

### GET /api/files/{id}/

Retrieve metadata for a previously uploaded file.

```bash
curl -s http://localhost:8000/api/files/3f2a1b4c5d6e7f8a9b0c1d2e3f4a5b6c/ | jq .
```

**Sample response (200)**

```json
{
  "id": "3f2a1b4c5d6e7f8a9b0c1d2e3f4a5b6c",
  "filename": "parcels.zip",
  "file_type": "shapefile",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "error": null,
  "created_at": "2024-06-01T08:00:00+00:00"
}
```

**404 example**

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "File 'badid' not found."
  }
}
```

---

### GET /api/files/{id}/measurements/

Returns paginated measurement results for every feature in the file.

**Query parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | int (1–500) | 50 | Page size |
| `offset` | int (≥ 0) | 0 | Skip this many features |
| `include_geometry` | bool | false | Include GeoJSON geometry dict in each item |

```bash
# First page, geometry included
curl -s "http://localhost:8000/api/files/3f2a1b4c5d6e7f8a9b0c1d2e3f4a5b6c/measurements/?limit=2&include_geometry=true" | jq .
```

**Sample response (200)**

```json
{
  "file_id": "3f2a1b4c5d6e7f8a9b0c1d2e3f4a5b6c",
  "total": 3,
  "limit": 2,
  "offset": 0,
  "summary": {
    "total_area_m2": 163296.48,
    "total_length_m": 0.0,
    "counts_by_status": {
      "COMPLETED": 3
    }
  },
  "items": [
    {
      "feature_index": 0,
      "geometry_type": "Polygon",
      "geometry": {
        "type": "Polygon",
        "coordinates": [[[9.0, 51.0], [9.01, 51.0], [9.01, 51.009], [9.0, 51.009], [9.0, 51.0]]]
      },
      "crs": "EPSG:4326",
      "properties": {
        "name": "Parcel A",
        "area_ha": "1.63"
      },
      "projected_crs": "EPSG:32632",
      "measurement_status": "COMPLETED",
      "measurements": {
        "area_m2": 54432.16,
        "area_ha": 5.4432,
        "perimeter_m": 929.44,
        "length_m": null,
        "length_km": null
      },
      "warnings": []
    },
    {
      "feature_index": 1,
      "geometry_type": "Polygon",
      "geometry": null,
      "crs": "EPSG:4326",
      "properties": {"name": "Parcel B"},
      "projected_crs": "EPSG:32632",
      "measurement_status": "COMPLETED",
      "measurements": {
        "area_m2": 54432.16,
        "area_ha": 5.4432,
        "perimeter_m": 929.44,
        "length_m": null,
        "length_km": null
      },
      "warnings": []
    }
  ]
}
```

**Zone-boundary warning example**

When a geometry spans a UTM zone boundary the `warnings` list contains a
human-readable explanation:

```json
"warnings": [
  "Geometry spans UTM zone boundary (zones 32–33); measurements projected into EPSG:32632 from representative point (11.9950°, 51.0005°). Consider splitting the feature for higher accuracy."
]
```

---

### GET /api/files/

List uploaded files, newest first.

```bash
curl -s "http://localhost:8000/api/files/?limit=5" | jq .
```

**Sample response (200)**

```json
[
  {
    "id": "3f2a1b4c5d6e7f8a9b0c1d2e3f4a5b6c",
    "filename": "parcels.zip",
    "file_type": "shapefile",
    "feature_count": 3,
    "crs": "EPSG:4326",
    "status": "COMPLETED",
    "error": null,
    "created_at": "2024-06-01T08:00:00+00:00"
  }
]
```

---

### Error envelope

Every error response (4xx / 5xx) uses the same JSON structure:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "CRS cannot be determined: .prj file is missing from the ZIP archive."
  }
}
```

| Field | Type | Description |
|---|---|---|
| `error.code` | string | Machine-readable code (see table in POST section) |
| `error.message` | string | Human-readable description |

---

## Architecture

### Application structure

```
geo-measure-api/
├── app/
│   ├── main.py                  # FastAPI app factory, lifespan, exception handlers
│   ├── config.py                # pydantic-settings: Settings, get_settings()
│   ├── api/
│   │   ├── routes/
│   │   │   └── files.py         # POST /files/, GET /files/{id}/, GET /files/{id}/measurements/
│   │   └── schemas.py           # Pydantic v2 request/response models + assembly helpers
│   ├── core/
│   │   └── errors.py            # AppError hierarchy + FastAPI exception handlers
│   ├── db/
│   │   ├── database.py          # SQLAlchemy async engine, get_db() dependency
│   │   ├── models.py            # File, Feature ORM models
│   │   └── repository.py       # FileRepository, FeatureRepository (CRUD)
│   └── services/
│       ├── ingestion.py         # validate_upload(), safe ZIP extraction, shapefile checks
│       ├── readers.py           # read_shapefile(), read_kml() → list[FeatureRecord]
│       ├── crs.py               # utm_crs_for_geometry(), reproject(), geodesic helpers
│       ├── measurement.py       # measure_feature(), measure_all() → MeasurementOutcome
│       └── processing.py        # FileProcessor — orchestrates read → measure → persist
├── tests/
│   ├── conftest.py              # Async fixtures (in-memory SQLite, AsyncClient)
│   ├── conftest_integration.py  # Sync fixtures (isolated env, TestClient, file factories)
│   ├── test_ingestion.py
│   ├── test_readers.py
│   ├── test_crs.py
│   ├── test_measurement.py
│   ├── test_processing.py
│   ├── test_schemas.py
│   ├── test_files_api.py
│   ├── test_health.py
│   ├── test_upload_and_measurements.py
│   └── test_integration.py      # Full-stack: HTTP → service → DB
├── Dockerfile                   # Multi-stage: builder + slim runtime, non-root user
├── docker-compose.yml           # Named volume, tmpfs scratch, bridge network
├── Makefile
├── pyproject.toml               # ruff + pytest + coverage config
└── requirements.txt
```

---

### File-processing flow

```
Client
  │
  │  POST /api/files/  (multipart)
  ▼
routes/files.py  upload_file()
  │
  ├─ 1. Read first chunk (64 KB)
  ├─ 2. validate_upload()           ← ingestion.py
  │      checks extension, Content-Length header, ZIP magic bytes / KML byte prefix
  │      raises 415 / 413 / 422 if bad  →  NO DB record created
  │
  ├─ 3. Stream remainder to disk   (enforces byte limit during write)
  │
  ├─ 4. FileRepository.create()    ← File row inserted, status = PROCESSING
  │
  ├─ 5. FileProcessor.process()    ← processing.py
  │      └─ asyncio.to_thread(_process_sync)   ← CPU-bound; event loop not blocked
  │           ├─ _safe_extract_zip()           ← ZIP security guards
  │           ├─ _find_shp() / _validate_shapefile_companions()
  │           ├─ read_shapefile() / read_kml() ← readers.py → list[FeatureRecord]
  │           └─ measure_all()                 ← measurement.py → list[MeasurementOutcome]
  │      └─ Back in async context:
  │           ├─ bulk_create Feature rows (with measurement results)
  │           └─ update_status(COMPLETED | FAILED)
  │
  ├─ 6. Upload file deleted from disk
  │
  └─ 7. Return FileUploadResponse (201)
```

**Failure handling**

- Any `AppError` subclass raised during processing marks the file `FAILED` and propagates the message into `File.error`. The HTTP status code matches the error type.
- Unexpected exceptions are wrapped in `ProcessingError` (422) and also mark the file `FAILED`.
- Per-feature exceptions inside `measure_all()` only mark that feature `FAILED`; the file itself completes.

---

### Measurement calculation flow

For each `FeatureRecord` from the reader:

```
measure_feature(geometry, source_crs)
  │
  ├─ Guard: null / empty  →  EMPTY
  ├─ Guard: GeometryCollection  →  UNSUPPORTED
  ├─ Guard: Point / MultiPoint  →  NOT_REQUIRED
  ├─ Guard: unknown type  →  UNSUPPORTED
  │
  ├─ _ensure_valid()
  │    shapely.is_valid?  yes → continue
  │                       no  → make_valid() → add warning → retry
  │                             still invalid → INVALID_GEOMETRY
  │
  ├─ utm_crs_for_geometry()       ← crs.py
  │    representative_point() → lon/lat → UTM/UPS EPSG
  │    bbox lon span → zone-boundary warning if crossing
  │
  ├─ reproject(geometry, source_crs, utm_crs)
  │
  └─ dispatch:
       Polygon/MultiPolygon  → area_m2, area_ha, perimeter_m
       Line/MultiLine        → length_m, length_km
```

All five metric keys (`area_m2`, `area_ha`, `perimeter_m`, `length_m`, `length_km`) are
always present in the response; inapplicable ones are `null`.

---

### CRS handling

**Selection is per-feature, not per-file.**  A single file may span multiple UTM zones
(e.g. a national road network); choosing one zone for the whole file would introduce
distortion in features far from that zone's central meridian.

**Algorithm** (implemented in [`app/services/crs.py`](app/services/crs.py)):

1. If `source_crs != EPSG:4326`, reproject the geometry to WGS-84 via a
   cached `pyproj.Transformer` (`always_xy=True`).
2. Compute `geometry.representative_point()` — guaranteed to lie inside the geometry,
   unlike `centroid` which can fall outside a concave polygon.
3. Select zone:
   - `|lat| ≤ 84°` → standard UTM — `EPSG:326xx` (north) / `EPSG:327xx` (south)
   - `|lat| > 84°` → polar UPS — `EPSG:32661` (Arctic) / `EPSG:32761` (Antarctic)
4. If the geometry's bounding-box longitude span crosses a 6° UTM boundary, a
   non-fatal warning is appended to the feature's `warnings` list.

**Transformer cache** — `pyproj.Transformer` objects are expensive to construct.
A module-level `dict[(src_crs, dst_crs) → Transformer]` ensures each pair is
created once per process lifetime, not once per feature.

**Norwegian / Svalbard UTM exceptions** (zones 32V, 33X, 35X, 37X) are intentionally
omitted. They apply only to a small geographic area; adding them would add branching
complexity with negligible real-world benefit for a general-purpose tool.
This is documented in the source code.

**Geodesic cross-check helpers** — `geodesic_area_m2()` and `geodesic_length_m()`
use `pyproj.Geod(ellps="WGS84").geometry_area_perimeter()` and
`.geometry_length()`. These are used in the test suite to assert that projected
measurements are within 1 % of the geodesic reference for small shapes (where the
comparison is meaningful).

---

## Design decisions

### Per-feature UTM vs single file-wide CRS vs geodesic-only vs equal-area

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Per-feature UTM** ✅ | Best local accuracy; each feature measured in its optimal zone | Slight implementation complexity; zone-crossing requires a warning | **Chosen** |
| Single file-wide CRS | Simpler; one reprojection per file | Distortion grows with distance from the zone's central meridian; wrong for global datasets | Rejected |
| Geodesic-only (`pyproj.Geod`) | No reprojection needed; globally correct | Cannot return perimeter without extra steps; harder to extend to length-by-segment | Rejected as primary; kept as test cross-check |
| Equal-area (e.g. Mollweide / LAEA) | Globally correct area | Conformal distortion makes perimeter/length inaccurate; awkward to pick CRS automatically | Rejected |

The per-feature UTM approach gives the best accuracy for the geometries that are
most commonly uploaded (city-scale parcels, regional road networks, national
boundary polygons).

---

### Synchronous processing vs background jobs

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Sync in threadpool** ✅ | Simple; one HTTP request = one complete response; no external services | Ties up a thread per upload; not suitable for very large files | **Chosen for v1** |
| Background worker (Celery/RQ) | Scales horizontally; can handle very large files | Requires a broker (Redis/RabbitMQ) and a separate worker process; complicates local dev | Deferred to Future scope |

The synchronous approach is correct and maintainable for the stated upload limits
(≤ 50 MB / ≤ 200 MB unzipped). The `FileProcessor` class is deliberately thin so
a background-worker implementation can satisfy the same interface without touching
the routes.

---

### SQLite vs PostgreSQL / PostGIS

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **SQLite + aiosqlite** ✅ | Zero infrastructure; single binary; works in Docker with a named volume; easy to test | No concurrent writes; no PostGIS geometry types; no horizontal DB scaling | **Chosen for v1** |
| PostgreSQL + PostGIS | Concurrent writes; native geometry indexing; ST_Area / ST_Length natively | Requires a running Postgres instance; more complex Docker setup; GDAL link must match | Deferred |

The database schema stores geometry as GeoJSON text (`Text` column) rather than a
PostGIS `geometry` type. This keeps the schema portable across databases and requires
no PostGIS extension — the cost is that spatial queries (e.g. "find all features
within a bounding box") cannot use a native index and would require loading all
geometry text and parsing it in Python.

---

### Reject missing `.prj` vs assume a CRS

A `.prj` file is the only reliable machine-readable source of the coordinate
reference system for a Shapefile. Without it:

- Assuming WGS-84 is wrong for the large majority of real-world Shapefiles, which
  are projected (e.g. national grid CRSs, state-plane systems).
- Measuring in the wrong CRS produces silently incorrect results — potentially
  off by orders of magnitude.
- There is no heuristic that can reliably detect the CRS from the coordinates alone.

**Decision:** reject with `422 VALIDATION_ERROR` and a message that names the
missing file. This is a hard failure, not a warning, because continuing would
produce meaningless measurements.

---

### Storing geometry as GeoJSON text vs PostGIS geometry

GeoJSON text was chosen for three reasons:

1. **Portability** — works with any SQL database without extensions.
2. **Simplicity** — the `Feature.geometry` column can be read and written with
   `json.loads` / `json.dumps`; no ORM type adapters needed.
3. **Testability** — in-memory SQLite databases work in CI without GDAL or PostGIS.

The trade-off is that spatial queries against the column are not indexable. This is
acceptable for v1 because the primary read pattern is "give me all features for this
file ID" (keyed by `file_id`, which is indexed).

---

## Limitations & accuracy

- **Single-writer SQLite** — concurrent uploads may queue on the SQLite write lock.
  This is expected behaviour for SQLite and is not a bug.
- **No authentication or authorisation** — any client can upload, read, or list files.
- **No rate limiting** — a client can submit an unlimited number of uploads.
- **Synchronous processing** — very large files (close to the 50 MB / 200 MB limits)
  will hold a server thread for the duration of processing.
- **Norwegian / Svalbard UTM exceptions** omitted (zones 32V, 33X, 35X, 37X). Features
  in those regions are assigned the geometrically correct (but officially incorrect) zone.
- **KML Z coordinates are dropped** — KML files often contain elevation (`0`) as a third
  coordinate; these are discarded before measurement.
- **No streaming upload progress** — the client must wait for the full 201 response.
- **Geometry stored as text** — spatial queries against the Feature table are not
  indexable without PostGIS.
- **No file deduplication** — uploading the same file twice creates two separate records.

### UTM planar vs geodesic accuracy

Measurements are calculated by projecting each feature into the nearest UTM zone and
computing planar (Euclidean) area and length on the projected surface.  
For typical city-scale to regional features this introduces **< 0.1% error** relative
to geodesic (ellipsoidal) reference values.  
For features larger than ~500 km or spanning a UTM zone boundary (6° longitude span)
planar distortion increases — the zone-boundary `warning` in the response flags these
cases. For globally-correct measurements use the `geodesic_area_m2()` / `geodesic_length_m()`
helpers in `app/services/crs.py` (which use `pyproj.Geod`) directly; they are
exposed as test cross-checks but not returned in the API response.

---

## Learnings

**`always_xy=True` is not optional.**  
`pyproj.Transformer` defaults to CRS-native axis order, which is `(lat, lon)` for
EPSG:4326. Without `always_xy=True` the longitude and latitude are silently swapped,
producing coordinates in the ocean for most real-world data. Setting `always_xy=True`
forces `(x=lon, y=lat)` consistently and is the correct default for spatial processing
pipelines.

**`representative_point()` is more reliable than `centroid()` for CRS selection.**  
The centroid of a concave polygon or a crescent-shaped MultiPolygon can fall outside
the geometry. `representative_point()` is guaranteed to return a point inside the
geometry, which means the zone selection is always geometrically meaningful.

**ZIP security is a real attack surface.**  
The "zip-slip" path traversal vulnerability (`../` in entry names) is a well-known
attack vector in any service that extracts ZIP files. The extraction code checks six
separate conditions — entry count, aggregate uncompressed size, absolute paths, `..`
in path components, symlinks, and post-resolve path containment — because each check
closes a different attack vector.

**Pydantic v2 `from_attributes=True` replaces `orm_mode`.**  
Pydantic v2 renamed the ORM configuration key. Code that worked under Pydantic v1
silently fails to populate models from ORM objects under v2 without this setting.

**SQLAlchemy 2 async sessions must be flushed, not committed, inside service methods.**  
The session commit is the route's responsibility (via the `get_db` dependency context).
Service methods call `flush()` to materialise IDs without ending the transaction,
keeping the entire file-processing pipeline inside a single atomic transaction.

**`asyncio.to_thread` is the correct bridge for synchronous CPU work in FastAPI.**  
GeoPandas, pyogrio, and pyproj release the GIL for most operations but still require
calling sync APIs. `asyncio.to_thread` offloads the call to the default
`ThreadPoolExecutor` and returns an awaitable, so the event loop is never blocked.

**In-memory SQLite needs `check_same_thread=False` for async tests.**  
SQLite's default thread-safety check raises an error when a connection created in one
thread (the test runner) is used from another (the async executor). Setting
`connect_args={"check_same_thread": False}` disables the check for test databases.

---

## Future scope

### Async background jobs (Celery / RQ + Redis)

The synchronous processing model works within the stated upload limits but does not
scale to very large files or high concurrency. The `FileProcessor` class is designed
as a thin interface so it can be replaced by a background-worker implementation:

- A Celery or RQ worker would pick up the `file_id` from a Redis queue.
- The route would return `202 Accepted` immediately with the file ID.
- The client would poll `GET /api/files/{id}/` until `status` changes from
  `PROCESSING` to `COMPLETED` or `FAILED`.

### PostgreSQL + PostGIS

Migrating from SQLite to PostgreSQL + PostGIS would enable:
- Concurrent writes without queueing.
- Native `geometry` columns with spatial indexes (`GIST`).
- Server-side spatial queries (`ST_Within`, `ST_Intersects`, bounding-box search).
- `ST_Area` / `ST_Length` as DB-level cross-checks.

The schema migration would require replacing `Text` geometry columns with PostGIS
`Geometry` types and updating the SQLAlchemy models with `geoalchemy2`.

### Additional formats

| Format | Notes |
|---|---|
| KMZ | A ZIP-wrapped KML — trivially supported by unzipping first |
| GeoJSON | Widely used; no CRS file needed (WGS-84 by convention) |
| GeoPackage (`.gpkg`) | Modern SQLite-based format; already supported by pyogrio |
| FlatGeobuf | Streaming-friendly binary format |
| GPS Exchange Format (`.gpx`) | Common for track/route data |

pyogrio can read all of these with no extra system dependencies.

### Streaming large files and chunked processing

For files significantly larger than the current limits, the service could:
- Accept a pre-signed S3 URL instead of a raw upload and download directly from
  object storage.
- Process features in batches and flush DB rows incrementally to avoid holding the
  entire dataset in memory.

### Authentication and authorisation

Options in order of complexity:
- API key in the `Authorization` header (simplest; suitable for internal tools).
- OAuth 2.0 / JWT (suitable for multi-tenant use).
- Per-user file isolation (each `File` row gains an `owner_id` FK).

### Rate limiting

A middleware layer using `slowapi` (a FastAPI-compatible limiter backed by Redis or
in-memory counters) can cap uploads per IP or per API key.

### S3 / object storage

Replacing the local `UPLOAD_DIR` with S3-compatible storage (AWS S3, MinIO, GCS)
would:
- Remove the dependency on a writable local filesystem (important for read-only
  container roots).
- Enable the API to run as multiple replicas behind a load balancer.
- Allow upload files to be retained for auditing rather than deleted after processing.

### Vector tile serving

Once features are stored in PostGIS, a vector tile endpoint (`/tiles/{z}/{x}/{y}.mvt`)
could be added using `pg_tileserv` or a Python implementation with `mapbox_vector_tile`.

### Measurement confidence / accuracy metadata

The response could include an `accuracy_note` field per feature indicating:
- Whether the feature spans a UTM zone boundary.
- The approximate distortion from projecting into a single UTM zone.
- Whether the geometry was repaired by `make_valid`.
