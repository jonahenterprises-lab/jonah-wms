"""Symmetric encryption for secrets stored at rest (OAuth tokens, banking API
keys). Separate from JWT_SECRET on purpose — a JWT-signing key and a
secrets-at-rest key have different blast radii if either one leaks."""
import os
from typing import Optional
from cryptography.fernet import Fernet, InvalidToken

_ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY")
if not _ENCRYPTION_KEY:
    raise RuntimeError(
        "ENCRYPTION_KEY environment variable must be set (see backend/.env). "
        "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )
_fernet = Fernet(_ENCRYPTION_KEY.encode() if isinstance(_ENCRYPTION_KEY, str) else _ENCRYPTION_KEY)


def encrypt_secret(plaintext: Optional[str]) -> Optional[str]:
    if not plaintext:
        return None
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: Optional[str]) -> Optional[str]:
    if not ciphertext:
        return None
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return None
