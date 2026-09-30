"""Validate and materialize request-scoped chat attachments."""

import base64
import binascii
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

MAX_ATTACHMENT_COUNT = 5
MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    content: bytes


def decode_uploaded_files(files: list[dict[str, str]]) -> list[UploadedFile]:
    """Validate transfer payloads before starting an agent response stream."""
    if len(files) > MAX_ATTACHMENT_COUNT:
        raise ValueError("Too many uploaded files")

    decoded: list[UploadedFile] = []
    total_bytes = 0
    for item in files:
        encoded = item.get("content_base64", "")
        filename = item.get("filename", "")
        if not isinstance(encoded, str) or not isinstance(filename, str) or not filename:
            raise ValueError("Invalid uploaded file")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("Invalid uploaded file") from error
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError("Uploaded file exceeds size limit")
        total_bytes += len(content)
        if total_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValueError("Uploaded files exceed size limit")
        decoded.append(UploadedFile(filename=filename, content=content))
    return decoded


@contextmanager
def materialize_uploaded_files(files: list[UploadedFile]) -> Iterator[dict[str, str]]:
    """Write uploads into an isolated temporary directory for one agent run."""
    if not files:
        yield {}
        return

    with tempfile.TemporaryDirectory(prefix="beanpilot_uploads_") as upload_dir:
        available: dict[str, str] = {}
        for index, uploaded_file in enumerate(files):
            suffix = Path(uploaded_file.filename).suffix
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
                suffix = ""
            alias = f"attachment-{index}{suffix}"
            path = os.path.join(upload_dir, alias)
            with open(path, "wb") as stream:
                stream.write(uploaded_file.content)
            available[alias] = path
        yield available
