import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.mandates import router as mandates_router
from app.core.config import get_settings
from app.services.redis_client import close_redis, get_redis

settings = get_settings()

logging.basicConfig(level=settings.log_level.upper())
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Starting ap2-merchant-gateway ...")
    # Warm-up Redis connection
    try:
        await get_redis()
        logger.info("Redis connection established.")
    except Exception as exc:
        logger.warning("Redis not reachable at startup: %s", exc)

    yield

    logger.info("Shutting down ...")
    await close_redis()


app = FastAPI(
    title="AP2 Merchant Gateway",
    description="Payment gateway service powered by Razorpay",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(mandates_router)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"service": settings.app_name, "status": "running"}
