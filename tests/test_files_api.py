"""Tests for the /api/files/ endpoints (GET list, GET detail, GET measurements)."""

from __future__ import annotations

from httpx import AsyncClient
import pytest

# ---------------------------------------------------------------------------
# List files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_files_empty(client: AsyncClient) -> None:
    response = await client.get("/api/files/")
    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# Get file — 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_file_not_found(client: AsyncClient) -> None:
    response = await client.get("/api/files/doesnotexist/")
    assert response.status_code == 404
    data = response.json()
    assert data["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Get measurements — 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_measurements_file_not_found(client: AsyncClient) -> None:
    response = await client.get("/api/files/doesnotexist/measurements/")
    assert response.status_code == 404
    data = response.json()
    assert data["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# List features (raw) — 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_features_file_not_found(client: AsyncClient) -> None:
    response = await client.get("/api/files/doesnotexist/features/")
    assert response.status_code == 404
    data = response.json()
    assert data["error"]["code"] == "NOT_FOUND"
