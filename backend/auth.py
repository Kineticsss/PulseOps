"""
PulseOps - backend/auth.py
===========================
API key generation, hashing, and verification.

CS 413 (Adv. Software Eng.) - Security by design:
  API keys are never stored in plaintext. We store a SHA-256 hash
  of the key — the same principle as password hashing.

  If the database leaks:
    - Attacker sees: hash strings (useless without the original key)
    - They cannot reverse a SHA-256 hash to get the original key
    - Real keys were only ever shown once (at registration time)

  Why SHA-256 and not bcrypt for API keys?
    bcrypt is intentionally slow (designed for passwords that humans
    type and attackers brute-force with dictionaries).
    API keys are long random strings (32 bytes = 256 bits of entropy)
    — impossible to brute-force regardless of hash speed.
    SHA-256 is fast and appropriate here.

  Why secrets.token_urlsafe()?
    The `secrets` module uses the OS's cryptographically secure
    random number generator (/dev/urandom on Linux, CryptGenRandom
    on Windows). Regular random.random() is NOT cryptographically
    secure — it is predictable if you know the seed.
"""

import hashlib
import secrets
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import text
from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# FastAPI security scheme — tells Swagger UI to show an "Authorize" button
# and extract the Bearer token from the Authorization header automatically
bearer_scheme = HTTPBearer()


def generate_api_key() -> tuple[str, str]:
    """
    Generates a new API key and returns (raw_key, hashed_key).

    The raw key is shown to the user ONCE and never stored.
    The hashed key is stored in the database.

    Format: pulseops_<32 random url-safe bytes>
    The prefix makes keys identifiable if found in logs or config files.
    """
    raw_key    = f"pulseops_{secrets.token_urlsafe(32)}"
    hashed_key = hash_key(raw_key)
    return raw_key, hashed_key


def hash_key(raw_key: str) -> str:
    """
    Returns the SHA-256 hex digest of the key.
    Deterministic — same input always produces same hash.
    Used both when storing and when verifying.
    """
    return hashlib.sha256(raw_key.encode()).hexdigest()


def verify_api_key(
    credentials: HTTPAuthorizationCredentials = Security(bearer_scheme),
    db: Session = None,
) -> dict:
    """
    Verifies the Bearer token against the database.
    Returns the server record if valid, raises 401 if not.

    CS 413 - Why we never return "key not found" vs "key invalid":
      Distinguishing between "this key doesn't exist" and "this key
      is revoked" gives attackers information about your key space.
      Always return the same 401 message regardless of the failure reason.
    """
    raw_key    = credentials.credentials
    hashed_key = hash_key(raw_key)

    server = db.execute(text("""
        SELECT server_id, is_active, registered_at
        FROM servers
        WHERE api_key_hash = :hash
    """), {"hash": hashed_key}).fetchone()

    if not server:
        _log_failed_auth(db, raw_key[:8] + "...", "key_not_found")
        raise HTTPException(
            status_code=401,
            detail="Invalid API key. Register this server at POST /api/servers/register"
        )

    if not server.is_active:
        _log_failed_auth(db, raw_key[:8] + "...", "key_revoked")
        raise HTTPException(
            status_code=401,
            detail="This API key has been revoked. Use POST /api/servers/{id}/rotate to issue a new one."
        )

    return {"server_id": server.server_id}


def _log_failed_auth(db: Session, key_prefix: str, reason: str):
    """
    Logs failed authentication attempts.
    Useful for detecting brute-force attempts or misconfigured agents.
    We log only the first 8 chars of the key — enough to identify
    which key was used without exposing the full value.
    """
    try:
        db.execute(text("""
            INSERT INTO auth_log (key_prefix, reason, attempted_at)
            VALUES (:prefix, :reason, :now)
        """), {
            "prefix": key_prefix,
            "reason": reason,
            "now":    datetime.now(timezone.utc),
        })
        db.commit()
    except Exception:
        pass  # Never let logging crash authentication
