"""Security utilities: JWT signing, password hashing, etc."""
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.hmac import HMAC


def generate_secret_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def compute_hmac_sha256(key: bytes, data: bytes) -> bytes:
    h = HMAC(key, hashes.SHA256())
    h.update(data)
    return h.finalize()


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def token_expiry(minutes: int = 60) -> datetime:
    return utcnow() + timedelta(minutes=minutes)
