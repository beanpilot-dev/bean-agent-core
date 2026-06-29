"""Response-language prompt helpers."""

import re

VALID_RESPONSE_LANGUAGES = {"zh-CN", "en", "auto"}
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z'-]*\b")
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_ACCOUNT_RE = re.compile(
    r"\b(?:Assets|Liabilities|Equity|Income|Expenses)(?::[^\s`'\",，。!！?？)）]+)+"
)
_PATH_RE = re.compile(r"(?:^|\s)(?:[\w.-]+/)+[\w.-]+")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_COMMODITY_RE = re.compile(r"\b[A-Z][A-Z0-9._-]{1,11}\b")


def infer_preferred_language(text: str | None) -> str:
    """Infer response language from latest user prose without an LLM call."""
    if not text:
        return "auto"
    prose = _strip_ledger_literals(text)
    if _HAN_RE.search(prose):
        return "zh-CN"
    if _LATIN_WORD_RE.search(prose):
        return "en"
    return "auto"


def normalize_preferred_language(value: str | None) -> str:
    """Normalize language values to the graph's supported set."""
    if not value:
        return "auto"
    normalized = value.strip()
    if normalized in VALID_RESPONSE_LANGUAGES:
        return normalized
    lowered = normalized.lower()
    if lowered in {"zh", "zh-cn", "chinese", "simplified chinese"}:
        return "zh-CN"
    if lowered in {"en", "english"}:
        return "en"
    return "auto"


def _latest_user_text(prompt: str | list | None) -> str:
    if isinstance(prompt, str):
        return prompt
    if not isinstance(prompt, list):
        return ""

    parts: list[str] = []
    for item in prompt:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text") or item.get("content")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _strip_ledger_literals(text: str) -> str:
    stripped = _FENCED_CODE_RE.sub(" ", text)
    stripped = _INLINE_CODE_RE.sub(" ", stripped)
    stripped = _ACCOUNT_RE.sub(" ", stripped)
    stripped = _PATH_RE.sub(" ", stripped)
    stripped = _DATE_RE.sub(" ", stripped)
    stripped = _COMMODITY_RE.sub(" ", stripped)
    return stripped


def detect_preferred_language(prompt: str | list | None) -> str:
    """Detect response language from the latest user prompt only.

    Ledger literals are removed before script detection so account names,
    commodities, paths, and Beancount snippets do not make the response language
    drift away from the user's prose.
    """
    return infer_preferred_language(_latest_user_text(prompt))


def response_language_instruction(preferred_language: str | None) -> str:
    """Return prompt text that controls natural-language response locale."""
    language = normalize_preferred_language(preferred_language)
    preservation = (
        "Preserve exact Beancount syntax, account names, commodities, file paths, "
        "tags, markdown code blocks, and machine-readable codes. Do not translate "
        "those literals."
    )
    if language == "zh-CN":
        return (
            "Respond in Simplified Chinese for natural-language prose. "
            f"{preservation}"
        )
    if language == "en":
        return (
            "Respond in English for natural-language prose. "
            f"{preservation}"
        )
    return (
        "Respond in the dominant natural language of the user's latest request. "
        f"{preservation}"
    )
