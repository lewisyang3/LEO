"""Encrypt OAuth tokens at rest with Fernet and `LEO_SECRET_KEY`."""

from cryptography.fernet import Fernet, InvalidToken

from leo.config import get_settings

GENERATE = (
    'uv run python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)


class SecretKeyMissing(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = get_settings().leo_secret_key
    if not key:
        raise SecretKeyMissing(f"set LEO_SECRET_KEY in .env; generate one with: {GENERATE}")
    return Fernet(key)


def encrypt(plaintext: bytes) -> bytes:
    return _fernet().encrypt(plaintext)


def decrypt(ciphertext: bytes) -> bytes:
    """Raises `InvalidToken` when `LEO_SECRET_KEY` isn't the key it was encrypted with."""
    return _fernet().decrypt(ciphertext)


__all__ = ["InvalidToken", "SecretKeyMissing", "decrypt", "encrypt"]
