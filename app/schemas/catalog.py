"""Pydantic v2 schemas for the public catalog & discovery endpoints."""
from __future__ import annotations

from pydantic import BaseModel


class CatalogProduct(BaseModel):
    sku: str
    name: str
    category: str
    unit_price_paise: int
    stock_qty: int
    currency: str

    model_config = {"from_attributes": True}
