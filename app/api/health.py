from fastapi import APIRouter
from sqlalchemy import text

from app.db.session import AsyncSessionLocal
from app.services.redis_client import get_redis

router = APIRouter(tags=["health"])


@router.get("/health", summary="Health check for DB and Redis connectivity")
async def health_check() -> dict[str, str]:
    status: dict[str, str] = {}

    # Check DB
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        status["db"] = "ok"
    except Exception as exc:
        status["db"] = f"error: {exc}"

    # Check Redis
    try:
        redis = await get_redis()
        pong = await redis.ping()
        status["redis"] = "ok" if pong else "no-response"
    except Exception as exc:
        status["redis"] = f"error: {exc}"

    return status
