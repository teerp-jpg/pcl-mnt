"""Storage validation uses bounded reads and never trusts upload filenames."""

import hashlib
import io
import uuid
from pathlib import Path

import pytest

from app.errors import APIError
from app.storage import MAX_ATTACHMENT_BYTES, LocalFileStorage, attachment_type, safe_filename


@pytest.mark.parametrize(
    "content,expected",
    [
        (b"%PDF-synthetic", "pdf"),
        (b"\xff\xd8\xffsynthetic", "jpeg"),
        (b"\x89PNG\r\n\x1a\nsynthetic", "png"),
    ],
)
def test_storage_round_trip(tmp_path: Path, content: bytes, expected: str) -> None:
    storage = LocalFileStorage(tmp_path)
    saved = storage.save(io.BytesIO(content))
    assert saved.content_type == expected and saved.size_bytes == len(content)
    assert saved.sha256 == hashlib.sha256(content).hexdigest()
    assert str(uuid.UUID(saved.key)) == saved.key
    with storage.open(saved.key) as source:
        assert source.read() == content


@pytest.mark.parametrize(
    "content", [b"", b"MZevil.pdf", b"<svg>not png</svg>", b"<html>not pdf</html>"]
)
def test_invalid_signature_leaves_no_blob(tmp_path: Path, content: bytes) -> None:
    with pytest.raises(APIError) as error:
        LocalFileStorage(tmp_path).save(io.BytesIO(content))
    assert error.value.code == "ATTACHMENT_INVALID" and not list(tmp_path.iterdir())


def test_exact_size_limit_and_oversize(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    content = b"%PDF-" + b"x" * (MAX_ATTACHMENT_BYTES - 5)
    assert storage.save(io.BytesIO(content)).size_bytes == MAX_ATTACHMENT_BYTES
    count = len(list(tmp_path.iterdir()))
    with pytest.raises(APIError):
        storage.save(io.BytesIO(content + b"x"))
    assert len(list(tmp_path.iterdir())) == count


def test_stream_is_not_read_whole_into_memory(tmp_path: Path) -> None:
    class BoundedStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            assert 0 <= size <= 65536
            return super().read(size)

    LocalFileStorage(tmp_path).save(BoundedStream(b"%PDF-" + b"x" * 200000))


@pytest.mark.parametrize("key", ["../outside", "/outside", "..\\outside", "not-uuid"])
def test_storage_rejects_arbitrary_paths(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError):
        LocalFileStorage(tmp_path).open(key)


def test_names_are_metadata_and_signatures_are_required() -> None:
    assert safe_filename("../../synthetic.pdf") == "synthetic.pdf"
    assert safe_filename("..\\synthetic\r\n.pdf") == "synthetic.pdf"
    assert safe_filename(None) == "attachment"
    with pytest.raises(APIError):
        attachment_type(b"fake image/png")
