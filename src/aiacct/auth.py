"""Password hashing and access tokens.

Accounts are seeded, never self-registered - a firm decides who works on its
clients' books, so there is deliberately no sign-up endpoint.

Identity is not decoration here. ``allocation.approved_by`` and
``correction.corrected_by`` record who signed off on a set of books, and
preparer/reviewer separation is ordinary accounting practice. A shared key
cannot answer "who approved this", which is why the API resolves a real user on
every request that changes something.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .config import Settings, get_settings
from .db.models import User
from .db.repo import Repositories

log = logging.getLogger(__name__)

# Argon2id at the library's defaults, which follow the current OWASP guidance.
_hasher = PasswordHasher()

ALGORITHM = "HS256"


class AuthError(Exception):
    """Raised for anything that should become a 401."""


# ---------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Check a password against a stored hash.

    Returns False rather than raising on a malformed or absent hash, so a user
    row seeded without a password simply cannot log in.
    """
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the hash was made with weaker parameters than current defaults."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


# ---------------------------------------------------------------- tokens


def create_access_token(user: User, settings: Settings | None = None) -> tuple[str, int]:
    """Issue a signed token. Returns the token and its lifetime in seconds."""
    settings = settings or get_settings()
    expires_in = settings.access_token_minutes * 60
    now = datetime.now(timezone.utc)

    payload = {
        "sub": str(user.id),
        "email": user.email,
        "name": user.name,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)
    return token, expires_in


def decode_access_token(token: str, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("token is not valid") from exc


# ---------------------------------------------------------------- flows


def authenticate(repos: Repositories, email: str, password: str) -> User:
    """Resolve an email and password to a user, or raise.

    The same message is returned whether the email is unknown or the password
    is wrong, so the endpoint cannot be used to discover which addresses exist.
    """
    user = repos.users.get_by_email(email.strip().lower())

    if user is None or not verify_password(password, user.password_hash):
        raise AuthError("incorrect email or password")
    if not user.is_active:
        raise AuthError("this account is no longer active")

    # Transparently upgrade a hash made with older parameters.
    if needs_rehash(user.password_hash):
        repos.users.set_password(user.id, hash_password(password))

    repos.users.record_login(user.id)
    return user


def user_from_token(repos: Repositories, token: str, settings: Settings | None = None) -> User:
    """Resolve a bearer token to the user it names.

    The database is consulted rather than trusting the token's claims, so
    deactivating an account takes effect immediately instead of at expiry.
    """
    payload = decode_access_token(token, settings)
    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthError("token is missing a usable subject") from exc

    user = repos.users.get(user_id)
    if user is None:
        raise AuthError("the user named by this token no longer exists")
    if not user.is_active:
        raise AuthError("this account is no longer active")
    return user
