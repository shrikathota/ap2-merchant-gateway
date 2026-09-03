"""
app/api/catalog.py
====================
Public read-only catalog endpoint — the second half of what an external
buyer agent needs to shop: discover the merchant via
/.well-known/agent-commerce.json (app/api/discovery.py), then fetch live
inventory from here to decide what to buy.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.catalog import Product
from app.schemas.catalog import CatalogProduct

router = APIRouter(prefix="/api", tags=["catalog"])


@router.get(
    "/catalog",
    response_model=list[CatalogProduct],
    summary="List the full live product catalog",
)
async def get_catalog(db: AsyncSession = Depends(get_db)) -> list[CatalogProduct]:
    stmt = select(Product).order_by(Product.category, Product.sku)
    result = await db.execute(stmt)
    products = result.scalars().all()
    return [CatalogProduct.model_validate(p) for p in products]
