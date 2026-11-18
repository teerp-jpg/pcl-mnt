import pytest

from app.config import ConfigurationError, Settings


def test_configuration_does_not_print_secrets(environment: dict[str, str]) -> None:
    settings = Settings.from_env(environment)
    representation = repr(settings)
    for name in ("DATABASE_URL", "JWT_SECRET", "SEED_ADMIN_PASSWORD", "SEED_ADMIN_EMAIL"):
        assert environment[name] not in representation
    assert settings.sandbox_seed is False
    assert settings.cors_origins == ("http://localhost:3100",)


@pytest.mark.parametrize(
    "name",
    [
        "DATABASE_URL",
        "APP_ENV",
        "SANDBOX_SEED",
        "JWT_SECRET",
        "SEED_ADMIN_EMAIL",
        "SEED_ADMIN_PASSWORD",
        "UPLOAD_ROOT",
        "CORS_ORIGINS",
    ],
)
@pytest.mark.parametrize("missing", [True, False])
def test_required_settings_fail_without_values(
    environment: dict[str, str], name: str, missing: bool
) -> None:
    if missing:
        environment.pop(name)
    else:
        environment[name] = " "
    with pytest.raises(ConfigurationError, match=f"Missing required setting: {name}$"):
        Settings.from_env(environment)


@pytest.mark.parametrize(
    "value",
    [
        "sqlite:///mtn.db",
        "postgresql://user:private@db/mtn",
        "postgresql+psycopg://user:private@db/",
        "postgresql+psycopg://user:private@db:invalid/mtn",
        "postgresql+psycopg://user:private@db:99999/mtn",
        "postgresql+psycopg://user:private@[broken/mtn",
    ],
)
def test_invalid_database_url_is_redacted(environment: dict[str, str], value: str) -> None:
    environment["DATABASE_URL"] = value
    with pytest.raises(ConfigurationError) as error:
        Settings.from_env(environment)
    assert str(error.value) == "Invalid setting: DATABASE_URL"
    assert value not in str(error.value)


@pytest.mark.parametrize(
    "value",
    [
        "*",
        '"http://localhost:3100"',
        '{"origin":"http://localhost:3100"}',
        '["*"]',
        "[null]",
        '["https://*.example.test"]',
        '["https://user:private@example.test"]',
        '["https://example.test/path"]',
        '["https://example.test?query=private"]',
        '["https://example.test#fragment"]',
        '["https://example.test:invalid"]',
        '["file:///tmp/private"]',
    ],
)
def test_credentialed_cors_rejects_invalid_origins(environment: dict[str, str], value: str) -> None:
    environment["CORS_ORIGINS"] = value
    with pytest.raises(ConfigurationError) as error:
        Settings.from_env(environment)
    assert str(error.value) == "Invalid setting: CORS_ORIGINS"


def test_cors_can_disable_browser_access(environment: dict[str, str]) -> None:
    environment["CORS_ORIGINS"] = "[]"
    assert Settings.from_env(environment).cors_origins == ()


@pytest.mark.parametrize("value", ["1", "0", "yes", "FALSE"])
def test_seed_requires_explicit_boolean(environment: dict[str, str], value: str) -> None:
    environment["SANDBOX_SEED"] = value
    with pytest.raises(ConfigurationError, match="Invalid setting: SANDBOX_SEED"):
        Settings.from_env(environment)


def test_read_environment_and_preserve_secret_whitespace(
    environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    environment["JWT_SECRET"] = " " + environment["JWT_SECRET"] + " "
    environment["SANDBOX_SEED"] = "true"
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.jwt_secret == environment["JWT_SECRET"]
    assert settings.sandbox_seed is True
