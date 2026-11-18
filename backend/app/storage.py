"""Immutable UUID blobs under the configured root; no user-controlled paths."""

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from app.errors import APIError

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
MIME_TYPES = {"pdf": "application/pdf", "jpeg": "image/jpeg", "png": "image/png"}


def attachment_type(header: bytes) -> str:
    if header.startswith(b"%PDF-"):
        return "pdf"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    raise APIError(422, "ATTACHMENT_INVALID", "Unsupported attachment signature")


@dataclass(frozen=True)
class StoredFile:
    key: str
    content_type: str
    size_bytes: int
    sha256: str


class FileStorage(Protocol):
    def save(self, source: BinaryIO) -> StoredFile: ...

    def open(self, key: str) -> BinaryIO: ...


class LocalFileStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        if str(uuid.UUID(key)) != key:
            raise ValueError("Invalid storage key")
        path = self.root / key
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ValueError("Unsafe storage path")
        return path

    def save(self, source: BinaryIO) -> StoredFile:
        # Validate size/signature before creating a blob; the multipart spool
        # is seekable. Copy in bounded chunks, never load the complete upload.
        source.seek(0, os.SEEK_END)
        size = source.tell()
        source.seek(0)
        content_type = attachment_type(source.read(16))
        source.seek(0)
        if size > MAX_ATTACHMENT_BYTES:
            raise APIError(422, "ATTACHMENT_INVALID", "Attachment exceeds size limit")
        self.root.mkdir(parents=True, exist_ok=True)
        key = str(uuid.uuid4())
        digest = hashlib.sha256()
        copied = 0
        with self._path(key).open("xb") as destination:
            while chunk := source.read(CHUNK_BYTES):
                copied += len(chunk)
                if copied > MAX_ATTACHMENT_BYTES:
                    raise APIError(422, "ATTACHMENT_INVALID", "Attachment exceeds size limit")
                digest.update(chunk)
                destination.write(chunk)
        if copied != size:
            raise APIError(422, "ATTACHMENT_INVALID", "Attachment changed during upload")
        return StoredFile(key, content_type, size, digest.hexdigest())

    def open(self, key: str) -> BinaryIO:
        # O_NOFOLLOW protects the final component on the Linux Sandbox too.
        path = self._path(key)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        return os.fdopen(os.open(path, flags), "rb")


def safe_filename(name: str | None) -> str:
    # Only display metadata; never used as a storage path or raw HTTP header.
    value = (name or "attachment").replace("\\", "/").rsplit("/", 1)[-1]
    value = "".join(char for char in value if char.isprintable()).strip()[:200]
    return value or "attachment"
