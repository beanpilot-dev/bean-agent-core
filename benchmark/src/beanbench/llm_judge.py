"""LLM judge for Tier 2 and Tier 3 evaluation."""

import json
import logging
import os

from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)


def _get_judge_llm(model: str, api_key: str, base_url: str | None = None) -> ChatOpenAI:
    kwargs = {
        "model": model,
        "api_key": api_key or "none",
        "temperature": 0,
    }
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)


def _call_judge(system_prompt: str, user_prompt: str, model: str, api_key: str, base_url: str | None) -> dict:
    llm = _get_judge_llm(model, api_key, base_url)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    resp = llm.invoke(messages)
    content = resp.content if hasattr(resp, "content") else str(resp)

    if isinstance(content, list):
        content = " ".join(str(item) for item in content)

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        json_match = __import__("re").search(r"\{[\s\S]*\}", content)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError:
                pass
        return {
            "score": 0,
            "matched_requirements": [],
            "missed_requirements": [],
            "fatal_errors": [],
            "reason": f"Failed to parse judge response: {content[:500]}",
        }


def _get_judge_config(model: str | None, api_key: str | None, base_url: str | None):
    return {
        "model": model or os.environ.get("BEANBENCH_JUDGE_MODEL", "gpt-4o"),
        "api_key": api_key or os.environ.get("BEANBENCH_JUDGE_API_KEY", ""),
        "base_url": base_url or os.environ.get("BEANBENCH_JUDGE_BASE_URL", ""),
    }


def judge_tier2(
    user_prompt: str,
    agent_response: str,
    fixture_facts: list[str],
    reference_answer: dict,
    judge_anchors: dict,
    system_prompt: str,
    judge_model: str | None = None,
    judge_api_key: str | None = None,
    judge_base_url: str | None = None,
) -> dict:
    """Judge a Tier 2 case using an LLM evaluator.

    Returns dict with keys: score, matched_requirements, missed_requirements,
    fatal_errors, reason, judge_model.
    """
    cfg = _get_judge_config(judge_model, judge_api_key, judge_base_url)

    expected_disposition = reference_answer.get("expected_disposition", "")
    required_facts = reference_answer.get("required_facts", [])
    example_answer = reference_answer.get("example_answer", "")

    fatal_errors = judge_anchors.get("fatal_errors", [])
    required_facts_anchor = judge_anchors.get("required_facts", [])

    user_prompt_text = f"""## User Request
{user_prompt}

## Fixture Facts
{chr(10).join(f'- {f}' for f in fixture_facts)}

## Expected Disposition
{expected_disposition}

## Required Facts (from reference answer)
{chr(10).join(f'- {f}' for f in required_facts)}

## Reference Answer (example)
{example_answer}

## Judge Anchors — Fatal Errors
{chr(10).join(f'- {f}' for f in fatal_errors)}

## Judge Anchors — Required Facts
{chr(10).join(f'- {f}' for f in required_facts_anchor)}

## Agent Response
{agent_response}

---

Score the agent response now. Return JSON only.
"""

    result = _call_judge(system_prompt, user_prompt_text, cfg["model"], cfg["api_key"], cfg["base_url"])
    result["judge_model"] = cfg["model"]
    return result


def judge_tier3(
    conversation_turns: list[dict],
    agent_final_response: str,
    reference_progress: list[str],
    final_reference_answer: dict,
    judge_anchors: dict,
    system_prompt: str,
    judge_model: str | None = None,
    judge_api_key: str | None = None,
    judge_base_url: str | None = None,
) -> dict:
    """Judge a Tier 3 case (multi-turn conversation) using an LLM evaluator.

    Returns dict with keys: score, semantic_score, state_score, stewardship_score,
    matched_requirements, missed_requirements, fatal_errors, reason, judge_model.
    """
    cfg = _get_judge_config(judge_model, judge_api_key, judge_base_url)

    conversation_text = []
    for i, turn in enumerate(conversation_turns):
        conversation_text.append(f"Turn {i + 1} ({turn.get('role', 'user')}): {turn.get('content', '')}")

    expected_disposition = final_reference_answer.get("expected_disposition", "")
    required_facts = final_reference_answer.get("required_facts", [])
    canonical_preview = final_reference_answer.get("canonical_preview", "")

    fatal_errors = judge_anchors.get("fatal_errors", [])
    required_facts_anchor = judge_anchors.get("required_facts", [])

    user_prompt_text = f"""## Conversation
{chr(10).join(conversation_text)}

## Reference Progress (expected per-turn behavior)
{chr(10).join(f'- {p}' for p in reference_progress)}

## Final Expected Disposition
{expected_disposition}

## Final Canonical Preview
{canonical_preview}

## Required Facts (from reference answer)
{chr(10).join(f'- {f}' for f in required_facts)}

## Judge Anchors — Fatal Errors
{chr(10).join(f'- {f}' for f in fatal_errors)}

## Judge Anchors — Required Facts
{chr(10).join(f'- {f}' for f in required_facts_anchor)}

## Agent Final Response
{agent_final_response}

---

Score the full conversation now. Return JSON only.
"""

    result = _call_judge(system_prompt, user_prompt_text, cfg["model"], cfg["api_key"], cfg["base_url"])
    result["judge_model"] = cfg["model"]
    return result
