"""
tests/test_phase7_backend.py
==============================
Phase 7 backend prerequisites for the external buyer agent:

  GET /.well-known/agent-commerce.json  — merchant discovery document
  GET /api/catalog                       — live product catalog
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.main import app
from app.models.catalog import Product

SQLITE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def db_engine():
    engine = create_async_engine(SQLITE_URL, echo=False)
    import app.models.audit  # noqa: F401
    import app.models.catalog  # noqa: F401
    import app.models.transaction  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def http_client(db_engine):
    from app.db.session import get_db as real_get_db

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[real_get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def seed_product(db_engine, sku, price=50_000, stock=10, category="books", name=None):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Product(sku=sku, name=name or f"Test {sku}", category=category,
                      unit_price_paise=price, stock_qty=stock))
        await s.commit()


class TestDiscoveryManifest:
    @pytest.mark.asyncio
    async def test_manifest_shape(self, http_client):
        r = await http_client.get("/.well-known/agent-commerce.json")
        assert r.status_code == 200
        data = r.json()
        assert data["protocol"] == "ap2"
        assert data["endpoints"]["transact"] == "/api/transact"
        assert data["endpoints"]["catalog"] == "/api/catalog"
        assert data["endpoints"]["audit_chain"] == "/api/audit/{intent_id}"
        assert "INSUFFICIENT_INVENTORY" in data["transact_lifecycle"]["recoverable_failure"]["reasons"]


class TestCatalogEndpoint:
    @pytest.mark.asyncio
    async def test_catalog_lists_products(self, http_client, db_engine):
        await seed_product(db_engine, sku="SHOE-001", price=250_000, stock=5, category="footwear",
                            name="Trail Runner")
        await seed_product(db_engine, sku="SHOE-002", price=0, stock=0, category="footwear",
                            name="Sold Out Sneaker")

        r = await http_client.get("/api/catalog")
        assert r.status_code == 200
        products = r.json()
        skus = {p["sku"] for p in products}
        assert {"SHOE-001", "SHOE-002"} <= skus
        shoe1 = next(p for p in products if p["sku"] == "SHOE-001")
        assert shoe1["name"] == "Trail Runner"
        assert shoe1["stock_qty"] == 5
        assert shoe1["unit_price_paise"] == 250_000

    @pytest.mark.asyncio
    async def test_catalog_empty_ok(self, http_client):
        r = await http_client.get("/api/catalog")
        assert r.status_code == 200
        assert r.json() == []
