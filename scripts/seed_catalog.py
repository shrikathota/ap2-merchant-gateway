#!/usr/bin/env python
"""
scripts/seed_catalog.py
=========================
Seeds a realistic-depth demo catalog: 5 categories x 6-10 SKUs each, with a
handful deliberately out-of-stock so the alternative-recovery path (Phase 5)
and the buyer agent's --force-failure demo (Phase 7) always have real
in-category substitutes to offer.

Idempotent: re-running replaces any existing rows with these SKUs rather
than duplicating them.

Usage:
    python scripts/seed_catalog.py
    make seed
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import delete  # noqa: E402

from app.db.session import AsyncSessionLocal  # noqa: E402
from app.models.catalog import Product  # noqa: E402

# (sku, name, category, price_paise, stock_qty)
CATALOG: list[tuple[str, str, str, int, int]] = [
    # --- footwear -----------------------------------------------------
    ("SHOE-NIMBUS-9", "Nimbus Runner (US 9)", "footwear", 250_000, 8),
    ("SHOE-TRAIL-9", "Trail Blazer (US 9)", "footwear", 280_000, 4),
    ("SHOE-SPRINT-9", "Sprint Pro (US 9)", "footwear", 299_000, 6),
    ("SHOE-VELOCITY-9", "Velocity X (US 9)", "footwear", 270_000, 0),  # OOS
    ("SHOE-CASUAL-9", "Weekend Slip-On (US 9)", "footwear", 189_000, 12),
    ("SHOE-HIKER-9", "Ridgeline Hiker (US 9)", "footwear", 340_000, 3),
    ("SHOE-NIMBUS-10", "Nimbus Runner (US 10)", "footwear", 250_000, 5),
    ("SHOE-TRAIL-10", "Trail Blazer (US 10)", "footwear", 280_000, 0),  # OOS

    # --- books ----------------------------------------------------------
    ("BOOK-ATOMIC-HABITS", "Atomic Habits", "books", 49_900, 20),
    ("BOOK-DEEP-WORK", "Deep Work", "books", 45_000, 15),
    ("BOOK-SAPIENS", "Sapiens: A Brief History of Humankind", "books", 55_000, 10),
    ("BOOK-CLEAN-CODE", "Clean Code", "books", 62_000, 7),
    ("BOOK-DESIGN-DATA", "Designing Data-Intensive Applications", "books", 89_000, 0),  # OOS
    ("BOOK-PSYCH-MONEY", "The Psychology of Money", "books", 39_900, 25),
    ("BOOK-THINKING-FAST", "Thinking, Fast and Slow", "books", 47_500, 9),

    # --- electronics ------------------------------------------------------
    ("ELEC-EARBUDS-X1", "SoundCore Earbuds X1", "electronics", 249_900, 14),
    ("ELEC-EARBUDS-PRO", "SoundCore Earbuds Pro ANC", "electronics", 399_900, 6),
    ("ELEC-CHARGER-65W", "GaN 65W Fast Charger", "electronics", 179_900, 30),
    ("ELEC-POWERBANK-20K", "20000mAh Power Bank", "electronics", 219_900, 0),  # OOS
    ("ELEC-SMARTWATCH-S1", "PulseFit Smartwatch S1", "electronics", 549_900, 5),
    ("ELEC-KEYBOARD-MECH", "Mechanical Keyboard 87-key", "electronics", 429_900, 8),
    ("ELEC-MOUSE-WIRELESS", "Wireless Mouse M2", "electronics", 89_900, 40),

    # --- apparel ----------------------------------------------------------
    ("APP-TSHIRT-CREW-M", "Everyday Crew Tee (M)", "apparel", 59_900, 22),
    ("APP-TSHIRT-CREW-L", "Everyday Crew Tee (L)", "apparel", 59_900, 18),
    ("APP-HOODIE-CHARCOAL-M", "Charcoal Pullover Hoodie (M)", "apparel", 149_900, 0),  # OOS
    ("APP-JACKET-WINDBREAKER", "Trail Windbreaker Jacket", "apparel", 219_900, 7),
    ("APP-JOGGERS-BLACK-M", "Performance Joggers (M)", "apparel", 129_900, 16),
    ("APP-CAP-RUNNING", "Running Cap (Adjustable)", "apparel", 39_900, 35),

    # --- home ------------------------------------------------------------
    ("HOME-MUG-CERAMIC", "Ceramic Coffee Mug 350ml", "home", 29_900, 50),
    ("HOME-BOTTLE-STEEL-1L", "Insulated Steel Bottle 1L", "home", 89_900, 20),
    ("HOME-LAMP-DESK-LED", "LED Desk Lamp (Dimmable)", "home", 159_900, 0),  # OOS
    ("HOME-CUSHION-THROW", "Throw Cushion Cover (Set of 2)", "home", 49_900, 25),
    ("HOME-DIFFUSER-AROMA", "Aroma Diffuser 300ml", "home", 129_900, 9),
    ("HOME-PLANTER-CERAMIC", "Ceramic Planter (Medium)", "home", 69_900, 14),
]


async def main() -> None:
    skus = [row[0] for row in CATALOG]
    async with AsyncSessionLocal() as session:
        await session.execute(delete(Product).where(Product.sku.in_(skus)))
        session.add_all(
            [
                Product(sku=sku, name=name, category=category, unit_price_paise=price, stock_qty=stock)
                for sku, name, category, price, stock in CATALOG
            ]
        )
        await session.commit()

    categories = sorted({row[2] for row in CATALOG})
    oos_count = sum(1 for row in CATALOG if row[4] == 0)
    print(f"Seeded {len(CATALOG)} products across {len(categories)} categories: {categories}")
    print(f"  {oos_count} deliberately out-of-stock (for the alternative-recovery demo)")


if __name__ == "__main__":
    asyncio.run(main())
