"""Response-language prompt helpers."""

VALID_RESPONSE_LANGUAGES = {"zh-CN", "en", "auto"}


def normalize_preferred_language(value: str | None) -> str:
    """Normalize planner language values to the graph's supported set."""
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
