"""PBKDF2 password storage using only the standard library."""

import hashlib
import hmac
import secrets

ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$")
        iterations = int(rounds)
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
        if (
            algorithm != "pbkdf2_sha256"
            or not ITERATIONS <= iterations <= 2_000_000
            or len(salt) != 16
            or len(expected) != 32
        ):
            return False
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


# Unknown accounts still perform the same expensive verification as known accounts.
DUMMY_HASH = hash_password(secrets.token_urlsafe(32))
