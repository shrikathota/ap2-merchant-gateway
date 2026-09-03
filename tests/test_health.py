"""
Smoke-tests for the /health endpoint.

These tests run against a running DB + Redis (integration mode).
For unit-test-only runs they are skipped if the services are unavailable.
"""
import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app


@pytest.mark.asyncio
async def test_health_returns_200() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_schema() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    data = response.json()
    assert "db" in data
    assert "redis" in data
