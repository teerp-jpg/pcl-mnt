"""Password storage contracts without database or application dependencies."""

import secrets

import pytest

from app.auth.passwords import ITERATIONS, hash_password, verify_password


def test_password_storage_uses_random_salt_and_verifies() -> None:
    password = secrets.token_urlsafe(24)
    first, second = hash_password(password), hash_password(password)
    assert first != second
    assert password not in first
    algorithm, rounds, salt, digest = first.split("$")
    assert algorithm == "pbkdf2_sha256" and int(rounds) >= ITERATIONS
    assert len(bytes.fromhex(salt)) == 16 and len(bytes.fromhex(digest)) == 32
    assert verify_password(password, first)
    assert not verify_password(password + "x", first)


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "invalid",
        "pbkdf2_sha256$0$00$00",
        "pbkdf2_sha256$999999999$00$00",
        "other$600000$00$00",
        "pbkdf2_sha256$600000$xx$zz",
    ],
)
def test_malformed_or_unbounded_hash_is_rejected(encoded: str) -> None:
    assert not verify_password("synthetic", encoded)
