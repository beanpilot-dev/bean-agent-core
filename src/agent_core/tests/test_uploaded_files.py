import base64

import pytest
from langchain_core.messages import HumanMessage

from agent_core.services.uploaded_files import (
    MAX_ATTACHMENT_BYTES,
    attachment_blocks,
    decode_uploaded_files,
)


def payload(filename: str = "statement.txt", content: bytes = b"date,amount\n"):
    return [{"filename": filename, "content_base64": base64.b64encode(content).decode()}]


def test_decode_uploaded_files_enforces_count_size_and_base64() -> None:
    assert decode_uploaded_files(payload())[0].content == b"date,amount\n"
    for value in [
        payload(content=b"x" * (MAX_ATTACHMENT_BYTES + 1)),
        [{"filename": "a.txt", "content_base64": "***"}],
        payload() * 6,
    ]:
        with pytest.raises(ValueError):
            decode_uploaded_files(value)


@pytest.mark.parametrize("extension", ["txt", "JSON", "xml"])
def test_text_is_complete_in_memory_data_without_parsing(extension: str) -> None:
    text = '<!DOCTYPE x SYSTEM "file:///etc/passwd">\n' + "你好\n" * 5000
    blocks = attachment_blocks(
        decode_uploaded_files(payload(f"../../private.{extension}", text.encode()))
    )
    assert text in blocks[0]["text"]
    assert "private" not in blocks[0]["text"]
    assert blocks[0]["type"] == "text"


@pytest.mark.parametrize(
    "name,content,mime",
    [
        ("image.png", b"\x89PNG\r\n\x1a\nsynthetic", "image/png"),
        ("image.jpeg", b"\xff\xd8\xffsynthetic", "image/jpeg"),
        ("image.webp", b"RIFF0000WEBPsynthetic", "image/webp"),
    ],
)
def test_image_is_a_multimodal_data_url(name, content, mime) -> None:
    block = attachment_blocks(decode_uploaded_files(payload(name, content)))[0]
    assert block == {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{base64.b64encode(content).decode()}"},
    }


@pytest.mark.parametrize(
    "name,content",
    [
        ("input.csv", b"a,b"),
        ("input.py", b"print(1)"),
        ("input.txt", b"\xff"),
        ("input.txt", b"nul\x00"),
        ("input.png", b"not an image"),
    ],
)
def test_rejects_unsupported_or_corrupt_attachment(name, content) -> None:
    with pytest.raises(ValueError):
        decode_uploaded_files(payload(name, content))


def test_history_strips_attachment_blocks_even_when_graph_copies_message() -> None:
    from agent_core.agent import _serialize_history

    original = HumanMessage(
        content=[{"type": "text", "text": "private attachment"}], id="transient"
    )
    copied = original.model_copy()
    assert _serialize_history([copied], transient_message=original, visible_query="Read this") == [
        {"role": "user", "content": "Read this"}
    ]


def test_file_import_is_rejected_without_opening_path(monkeypatch) -> None:
    from agent_core.services.mutations.handlers.bulk_commit import BulkCommitPreparationHandler

    monkeypatch.setattr(
        "builtins.open", lambda *args, **kwargs: pytest.fail("File input must never be opened")
    )
    result = BulkCommitPreparationHandler().build("/unavailable", transactions_file="/etc/passwd")
    assert result.invariant == "UNSUPPORTED_FILE_INPUT"
