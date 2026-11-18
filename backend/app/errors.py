"""Consistent API errors without reflecting request bodies or exception values."""

from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]


class APIError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, JSONValue] | None = None,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details if details is not None else {}


async def api_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, APIError):
        return await unexpected_error_handler(_request, exc)
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message, "details": exc.details},
    )


async def validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        return await unexpected_error_handler(_request, exc)
    # Pydantic's input/ctx/msg can contain passwords or custom exception values.
    errors = [
        {"location": [str(part) for part in error["loc"]], "type": error["type"]}
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "code": "VALIDATION_FAILED",
            "message": "Request validation failed",
            "details": {"errors": errors},
        },
    )


async def http_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, HTTPException):
        return await unexpected_error_handler(_request, exc)
    codes = {401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND"}
    try:
        message = HTTPStatus(exc.status_code).phrase
    except ValueError:
        message = "HTTP request failed"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": codes.get(exc.status_code, "HTTP_ERROR"),
            "message": message,
            "details": {},
        },
        headers=exc.headers,
    )


async def unexpected_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "code": "INTERNAL_ERROR",
            "message": "Internal server error",
            "details": {},
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(APIError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)
