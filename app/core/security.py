"""Password hashing, password policy, OTPs and one-time tokens."""
import hashlib
import hmac
import re
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from flask import current_app

from app.core.errors import ValidationError

_hasher = PasswordHasher()


def hash_password(raw):
    return _hasher.hash(raw)


def verify_password(hashed, raw):
    if not hashed:
        return False
    try:
        return _hasher.verify(hashed, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def validate_password_policy(raw, previous_hashes=()):
    cfg = current_app.config
    problems = []
    if len(raw or "") < cfg["PASSWORD_MIN_LENGTH"]:
        problems.append(f"at least {cfg['PASSWORD_MIN_LENGTH']} characters")
    if cfg["PASSWORD_REQUIRE_UPPER"] and not re.search(r"[A-Z]", raw or ""):
        problems.append("an upper-case letter")
    if cfg["PASSWORD_REQUIRE_LOWER"] and not re.search(r"[a-z]", raw or ""):
        problems.append("a lower-case letter")
    if cfg["PASSWORD_REQUIRE_DIGIT"] and not re.search(r"\d", raw or ""):
        problems.append("a digit")
    if cfg["PASSWORD_REQUIRE_SYMBOL"] and not re.search(r"[^A-Za-z0-9]", raw or ""):
        problems.append("a symbol")
    if problems:
        raise ValidationError("Password must contain " + ", ".join(problems),
                              details={"field": "password"})
    for old in list(previous_hashes)[: cfg["PASSWORD_HISTORY_COUNT"]]:
        if verify_password(old, raw):
            raise ValidationError(
                f"Password must not match any of your last {cfg['PASSWORD_HISTORY_COUNT']} passwords",
                details={"field": "password"})


def _hmac(value):
    key = current_app.config["SECRET_KEY"].encode()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def generate_otp():
    length = current_app.config["OTP_LENGTH"]
    return "".join(secrets.choice("0123456789") for _ in range(length))


def hash_otp(code):
    return _hmac("otp:" + code)


def generate_token():
    """Opaque URL-safe token; only its hash is stored."""
    return secrets.token_urlsafe(32)


def hash_token(token):
    return _hmac("token:" + token)


def constant_time_equals(a, b):
    return hmac.compare_digest(a or "", b or "")
