# ============================================================
# Stage 1 — builder
#   Installs GDAL system libraries and all Python dependencies
#   into a dedicated prefix so only the results are copied out.
# ============================================================
FROM python:3.12-slim AS builder

# System deps required by GDAL / pyproj at build time
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gdal-bin \
        libgdal-dev \
        libgeos-dev \
        libproj-dev \
        libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only the requirements file first so Docker can cache
# this layer separately from the application code.
COPY requirements.txt .

# Install into an isolated prefix — we'll copy it to the runtime stage
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ============================================================
# Stage 2 — runtime
#   Slim image with only what is needed to run the application.
# ============================================================
FROM python:3.12-slim AS runtime

# Runtime-only shared libraries needed by GDAL / pyproj / Shapely
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gdal-bin \
        libgdal-dev \
        libgeos-dev \
        libproj-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ---- Non-root user ----
RUN groupadd --gid 1001 appgroup \
    && useradd --uid 1001 --gid appgroup --no-create-home --shell /bin/false appuser

# Copy installed Python packages from the builder stage
COPY --from=builder /install /usr/local

# Working directory (owned by root; only /data is written at runtime)
WORKDIR /app

# Copy application source
COPY --chown=appuser:appgroup app/ ./app/

# Create the data directory and give appuser ownership
RUN mkdir -p /data/uploads && chown -R appuser:appgroup /data

# ---- Runtime environment ----
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DATABASE_URL=sqlite+aiosqlite:////data/geo_measure.db \
    UPLOAD_DIR=/data/uploads \
    MAX_UPLOAD_MB=50 \
    MAX_UNZIPPED_MB=200 \
    MAX_ZIP_ENTRIES=50

# Persist SQLite DB and uploads across container restarts
VOLUME ["/data"]

EXPOSE 8000

USER appuser

# ---- Healthcheck ----
# Uses the /health endpoint.  curl is installed above.
HEALTHCHECK \
    --interval=30s \
    --timeout=5s \
    --start-period=10s \
    --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--log-level", "info"]
