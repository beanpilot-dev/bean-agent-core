import base64
import os

import pytest

from agent_core.services.uploaded_files import (
    MAX_ATTACHMENT_BYTES,
    UploadedFile,
    decode_uploaded_files,
    materialize_uploaded_files,
)


def payload(filename: str = "statement.csv", content: bytes = b"date,amount\n"):
    return [{"filename": filename, "content_base64": base64.b64encode(content).decode()}]


def test_decode_uploaded_files_enforces_count_size_and_base64() -> None:
    assert decode_uploaded_files(payload())[0].content == b"date,amount\n"

    with pytest.raises(ValueError):
        decode_uploaded_files(payload(content=b"x" * (MAX_ATTACHMENT_BYTES + 1)))
    with pytest.raises(ValueError):
        decode_uploaded_files([{"filename": "a.csv", "content_base64": "***"}])
    with pytest.raises(ValueError):
        decode_uploaded_files(payload() * 6)


def test_materialize_uploaded_files_uses_safe_names_and_cleans_temp_directory() -> None:
    uploaded = [UploadedFile(filename="../../private.csv", content=b"synthetic")]

    with materialize_uploaded_files(uploaded) as available:
        path = available["attachment-0.csv"]
        assert os.path.basename(path) == "attachment-0.csv"
        with open(path, "rb") as stream:
            assert stream.read() == b"synthetic"
        temp_dir = os.path.dirname(path)

    assert not os.path.exists(temp_dir)


def test_materialize_uploaded_files_cleans_up_after_exception() -> None:
    temp_dir = None
    with pytest.raises(RuntimeError):
        with materialize_uploaded_files([UploadedFile("input.txt", b"synthetic")]) as available:
            temp_dir = os.path.dirname(available["attachment-0.txt"])
            raise RuntimeError("simulated request cancellation")

    assert temp_dir is not None
    assert not os.path.exists(temp_dir)
