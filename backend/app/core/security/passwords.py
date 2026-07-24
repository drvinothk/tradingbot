"""Argon2 password hashing. Never roll your own — argon2-cffi's defaults are
sane (time_cost/memory_cost tuned for interactive login, not high throughput)."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        _hasher.verify(hashed, plain)
    except VerifyMismatchError:
        return False
    return True


def needs_rehash(hashed: str) -> bool:
    """True if the hash was made with older/weaker parameters than current
    defaults — call after a successful verify and rehash+save if True."""
    return _hasher.check_needs_rehash(hashed)
