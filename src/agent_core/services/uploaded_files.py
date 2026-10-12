"""Validate bounded attachments and build transient multimodal message blocks."""

import base64
import binascii
from dataclasses import dataclass
from pathlib import Path

MAX_ATTACHMENT_COUNT = 5
MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 10 * 1024 * 1024
TEXT_EXTENSIONS = {".txt", ".json", ".xml"}
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    content: bytes


def attachment_block(file: UploadedFile, index: int) -> dict:
    """Treat text as data, without executing code or resolving XML entities."""
    suffix = Path(file.filename).suffix.lower()
    label = f"attachment-{index}{suffix}"
    if suffix in TEXT_EXTENSIONS:
        text = file.content.decode("utf-8-sig", errors="strict")
        if "\x00" in text:
            raise ValueError("Invalid text attachment")
        return {
            "type": "text",
            "text": f"[Attachment data: {label}]\n{text}\n[End attachment data]",
        }
    mime = IMAGE_TYPES.get(suffix)
    valid = (
        suffix == ".png"
        and file.content.startswith(b"\x89PNG\r\n\x1a\n")
        or suffix in {".jpg", ".jpeg"}
        and file.content.startswith(b"\xff\xd8\xff")
        or suffix == ".webp"
        and file.content.startswith(b"RIFF")
        and file.content[8:12] == b"WEBP"
    )
    if not mime or not valid:
        raise ValueError("Unsupported attachment format")
    encoded = base64.b64encode(file.content).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def decode_uploaded_files(files: list[dict[str, str]]) -> list[UploadedFile]:
    """Reject unsupported/corrupt inputs before starting an agent stream."""
    if len(files) > MAX_ATTACHMENT_COUNT:
        raise ValueError("Too many uploaded files")
    decoded: list[UploadedFile] = []
    total_bytes = 0
    for item in files:
        encoded = item.get("content_base64", "")
        filename = item.get("filename", "")
        if not isinstance(encoded, str) or not isinstance(filename, str) or not filename:
            raise ValueError("Invalid uploaded file")
        # Bound the encoded representation before allocating decoded bytes.
        if len(encoded) > 4 * ((MAX_ATTACHMENT_BYTES + 2) // 3):
            raise ValueError("Uploaded file exceeds size limit")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("Invalid uploaded file") from error
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError("Uploaded file exceeds size limit")
        total_bytes += len(content)
        if total_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValueError("Uploaded files exceed size limit")
        file = UploadedFile(filename=filename, content=content)
        attachment_block(file, len(decoded))
        decoded.append(file)
    return decoded


def attachment_blocks(files: list[UploadedFile]) -> list[dict]:
    return [attachment_block(file, index) for index, file in enumerate(files)]
