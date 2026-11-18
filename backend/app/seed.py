"""Idempotent synthetic administrator bootstrap; business seed belongs to T12."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.passwords import hash_password
from app.auth.schemas import normalize_email
from app.auth.users import audit_user, lock_user_writes
from app.config import ConfigurationError, Settings
from app.db import Database
from app.models import AppUser


def seed_admin(session: Session, settings: Settings) -> AppUser | None:
    if not settings.sandbox_seed:
        return None
    if settings.app_env != "sandbox":
        raise ConfigurationError("Admin seed requires APP_ENV=sandbox")
    try:
        email = normalize_email(settings.seed_admin_email)
    except ValueError:
        raise ConfigurationError("Invalid setting: SEED_ADMIN_EMAIL") from None
    lock_user_writes(session)
    user = session.execute(select(AppUser).where(AppUser.email == email)).scalar_one_or_none()
    if user is not None:
        # Preserve existing password, role, active state and user-edited profile.
        return user
    if not 12 <= len(settings.seed_admin_password) <= 1024:
        raise ConfigurationError("Invalid setting: SEED_ADMIN_PASSWORD")
    user = AppUser(
        email=email,
        display_name="ผู้ดูแล Sandbox (ข้อมูลสมมติ)",
        role="admin",
        is_active=True,
        password_hash=hash_password(settings.seed_admin_password),
    )
    session.add(user)
    session.flush()
    user.created_by = user.id
    user.updated_by = user.id
    audit_user(session, user, user, "SEED_ADMIN", None)
    session.commit()
    return user


def main() -> None:
    settings = Settings.from_env()
    if not settings.sandbox_seed:
        return
    database = Database(settings.database_url)
    try:
        with database.sessions() as session:
            seed_admin(session, settings)
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
