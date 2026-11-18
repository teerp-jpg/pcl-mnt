import secrets

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, field_validator
from starlette.exceptions import HTTPException

from app.errors import APIError, register_error_handlers


def test_error_envelope_and_secret_redaction() -> None:
    app = FastAPI()
    register_error_handlers(app)
    private_value = secrets.token_urlsafe(24)

    class Input(BaseModel):
        password: str

        @field_validator("password")
        @classmethod
        def reject_password(cls, value: str) -> str:
            raise ValueError(value)

    @app.post("/validate")
    def validate(_payload: Input) -> None:
        pass

    @app.get("/business")
    def business() -> None:
        raise APIError(409, "VERSION_CONFLICT", "Version conflict", {"version": 2})

    @app.get("/http")
    def http() -> None:
        raise HTTPException(401, private_value, headers={"WWW-Authenticate": "Bearer"})

    @app.get("/unexpected")
    def unexpected() -> None:
        raise RuntimeError(private_value)

    with TestClient(app, raise_server_exceptions=False) as client:
        invalid = client.post("/validate", json={"password": private_value})
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "VALIDATION_FAILED"
        assert invalid.json()["details"]["errors"] == [
            {"location": ["body", "password"], "type": "value_error"}
        ]
        assert private_value not in invalid.text

        conflict = client.get("/business")
        assert conflict.status_code == 409
        assert conflict.json() == {
            "code": "VERSION_CONFLICT",
            "message": "Version conflict",
            "details": {"version": 2},
        }

        unauthorized = client.get("/http")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["code"] == "UNAUTHENTICATED"
        assert unauthorized.headers["www-authenticate"] == "Bearer"
        assert private_value not in unauthorized.text

        failed = client.get("/unexpected")
        assert failed.status_code == 500
        assert failed.json() == {
            "code": "INTERNAL_ERROR",
            "message": "Internal server error",
            "details": {},
        }
        assert private_value not in failed.text

        missing = client.get("/missing")
        assert missing.status_code == 404
        assert missing.json()["code"] == "NOT_FOUND"
