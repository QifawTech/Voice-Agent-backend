"""
Voice Agent Platform — Security Utilities

Provides all security functions:
- Password hashing (bcrypt)
- JWT token creation and verification
- API key generation (for widgets)
- AES-256 encryption (for storing API tokens at rest)

Usage:
    from app.services.security import (
        hash_password, verify_password,
        create_access_token, verify_access_token,
        generate_api_key,
        encrypt_token, decrypt_token, looks_encrypted,
    )
"""

import secrets
import base64
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings
from app.logger import get_logger

logger = get_logger(__name__)

# ---- Password Hashing (bcrypt) ----

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    """
    Hash a plain-text password using bcrypt.

    Args:
        plain_password: The user's raw password.

    Returns:
        Hashed password string (e.g., "$2b$12$...").
    """
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plain-text password against its bcrypt hash.

    Args:
        plain_password: The password attempt.
        hashed_password: The stored hash from the database.

    Returns:
        True if the password matches, False otherwise.
    """
    return pwd_context.verify(plain_password, hashed_password)


# ---- JWT Token ----


def create_access_token(
    data: dict,
    expires_delta: timedelta | None = None,
) -> str:
    """
    Create a JWT access token.

    Args:
        data: Payload to encode (e.g., {"sub": user_id, "role": "tenant_admin"}).
        expires_delta: Custom expiry. Defaults to JWT_EXPIRY_MINUTES from config.

    Returns:
        Encoded JWT string.
    """
    settings = get_settings()

    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.JWT_EXPIRY_MINUTES
        )

    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})

    encoded_jwt = jwt.encode(
        to_encode,
        settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )

    logger.info("jwt_token_created", sub=data.get("sub"))
    return encoded_jwt


def verify_access_token(token: str) -> dict | None:
    """
    Verify and decode a JWT access token.

    Args:
        token: The JWT string to verify.

    Returns:
        Decoded payload dict if valid, None if invalid/expired.
    """
    settings = get_settings()

    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        return payload
    except JWTError as e:
        logger.warning("jwt_verification_failed", error=str(e))
        return None


# ---- API Key Generation ----


def generate_api_key(prefix: str = "sk_live") -> str:
    """
    Generate a secure, unique API key for widget authentication.

    Format: sk_live_<32 random hex characters>
    Example: sk_live_7f8a2b3c4d5e6f7a8b9c0d1e2f3a4b5c

    Args:
        prefix: Key prefix (e.g., "sk_live", "sk_test").

    Returns:
        API key string.
    """
    random_part = secrets.token_hex(16)  # 32 hex characters
    api_key = f"{prefix}_{random_part}"
    logger.info("api_key_generated", prefix=prefix)
    return api_key


# ---- AES-256 Encryption (for API tokens at rest) ----


def _get_fernet() -> Fernet:
    """
    Create a Fernet encryption instance from the ENCRYPTION_KEY config.

    Uses PBKDF2 to derive a proper 32-byte key from the config string,
    then base64-encodes it for Fernet.
    """
    settings = get_settings()
    key_bytes = settings.ENCRYPTION_KEY.encode("utf-8")

    # Derive a proper 32-byte key using PBKDF2
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"voice-agent-salt",  # Fixed salt (key is already unique per deployment)
        iterations=100_000,
    )
    derived_key = base64.urlsafe_b64encode(kdf.derive(key_bytes))

    return Fernet(derived_key)


# Fernet v1 tokens are urlsafe-base64 of a payload whose first byte is the
# version marker 0x80, which always renders as the prefix "gAAAAA".
_FERNET_PREFIX = "gAAAAA"


def looks_encrypted(value: str | None) -> bool:
    """
    Report whether a stored value looks like a Fernet ciphertext.

    Lets callers tell "this was never encrypted" (a hand-seeded or legacy
    plaintext row) apart from "this is ciphertext we cannot open" (wrong or
    rotated ENCRYPTION_KEY) — two very different operational problems that
    otherwise surface as the same opaque failure.

    This is a shape check, not a validity check; decryption still has to be
    attempted.
    """
    return bool(value) and value.startswith(_FERNET_PREFIX)


def encrypt_token(plain_token: str) -> str:
    """
    Encrypt a plain-text API token for safe database storage.

    Args:
        plain_token: The raw API token (e.g., a restaurant's bearer token).

    Returns:
        Encrypted string (base64-encoded).
    """
    fernet = _get_fernet()
    encrypted = fernet.encrypt(plain_token.encode("utf-8"))
    return encrypted.decode("utf-8")


def decrypt_token(encrypted_token: str) -> str:
    """
    Decrypt an encrypted API token from the database.

    Args:
        encrypted_token: The encrypted string from the database.

    Returns:
        Decrypted plain-text token.

    Raises:
        ValueError: If decryption fails (wrong key or corrupted data).
    """
    try:
        fernet = _get_fernet()
        decrypted = fernet.decrypt(encrypted_token.encode("utf-8"))
        return decrypted.decode("utf-8")
    except Exception as e:
        logger.error("token_decryption_failed", error=str(e))
        raise ValueError("Failed to decrypt token") from e
