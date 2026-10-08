"""Tests for the /health endpoint and global error handler."""

from __future__ import annotations

from httpx import AsyncClient
import pytest


@pytest.mark.asyncio
async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "version" in data


@pytest.mark.asyncio
async def test_health_response_shape(client: AsyncClient) -> None:
    """Ensure the health response has no extra unexpected keys."""
    response = await client.get("/health")
    data = response.json()
    assert set(data.keys()) == {"status", "version"}


@pytest.mark.asyncio
async def test_not_found_uses_error_envelope(client: AsyncClient) -> None:
    """404s from FastAPI itself should still arrive as JSON (not the default HTML)."""
    response = await client.get("/api/files/nonexistent-id-that-wont-exist/")
    assert response.status_code == 404
    data = response.json()
    assert "error" in data
    assert "code" in data["error"]
    assert "message" in data["error"]
