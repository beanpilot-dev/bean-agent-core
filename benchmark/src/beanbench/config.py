"""Load and parse BeanBench YAML configs into typed models."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


class PostingAssertion(BaseModel):
    account: str
    units: str
    currency: str
    cost: str | None = None
    unit_price: str | None = None


class DeterministicAssertions(BaseModel):
    entry_type: str
    date: str
    posting_multiset: list[PostingAssertion] = []
    required_tags: list[str] = []
    required_links: list[str] = []
    required_metadata: dict[str, str] = {}
    forbidden_account_prefixes: list[str] = []
    price_assertion: dict[str, str] | None = None


class ReferenceAnswer(BaseModel):
    expected_disposition: str
    canonical_preview: str = ""
    required_facts: list[str] = []
    example_answer: str = ""
    accepted_variation: list[str] = []
    omitted_items: list[str] = []


class JudgeAnchor(BaseModel):
    fatal_errors: list[str] = []
    required_facts: list[str] = []


class Tier1Case(BaseModel):
    id: str
    fixture: str
    title: str
    user_prompt: str
    reference_answer: ReferenceAnswer
    deterministic_assertions: DeterministicAssertions


class Tier2Case(BaseModel):
    id: str
    fixture: str
    title: str
    user_prompt: str
    fixture_facts: list[str] = []
    reference_answer: ReferenceAnswer
    judge_anchors: JudgeAnchor


class Tier3Turn(BaseModel):
    role: str
    content: str


class Tier3Case(BaseModel):
    id: str
    fixture: str
    title: str
    turns: list[Tier3Turn]
    reference_progress: list[str] = []
    final_reference_answer: ReferenceAnswer
    judge_anchors: JudgeAnchor


class FixtureConfig(BaseModel):
    root: str
    entry_file: str
    allowed_preview_paths: list[str]
    policy: dict[str, Any]
    allowed_accounts: list[str] = []
    known_prices: list[dict[str, str]] = []
    known_lots: list[dict[str, Any]] = []
    known_entries: list[dict[str, Any]] = []
    available_imports: list[dict[str, Any]] = []
    existing_duplicate_candidates: list[dict[str, Any]] = []
    includes: list[str] = []


class TierScoring(BaseModel):
    points_per_case: int
    evaluator: str


class TierMeta(BaseModel):
    id: str
    name: str
    maximum_score: int
    scoring: TierScoring
    case_file: str


class JudgeModelSettings(BaseModel):
    temperature: float = 0
    response_format: str = "json"


class JudgeEvalConfig(BaseModel):
    judge_model_settings: JudgeModelSettings
    system_prompt: str


class BenchmarkConfig(BaseModel):
    """Top-level configuration from requirements.yaml."""

    benchmark_id: str
    benchmark_name: str
    benchmark_version: str = "1.0"
    fixtures: dict[str, FixtureConfig]
    tiers: dict[str, TierMeta]
    tier_2_judge: JudgeEvalConfig
    tier_3_judge: JudgeEvalConfig
    global_invariant_ids: list[str]

    @classmethod
    def from_yaml(cls, yaml_path: Path) -> "BenchmarkConfig":
        with open(yaml_path) as f:
            data = yaml.safe_load(f)

        schema = data.get("schema_version", "1.0")
        bench = data["benchmark"]

        fixtures_raw = data.get("fixtures", {})
        fixtures: dict[str, FixtureConfig] = {}
        for key, val in fixtures_raw.items():
            fixtures[key] = FixtureConfig(**val)

        tiers_raw = data.get("tiers", {})
        tiers: dict[str, TierMeta] = {}
        for key, val in tiers_raw.items():
            tiers[key] = TierMeta(**val)

        t2_judge_raw = data.get("evaluation", {}).get("tier_2_llm_judge", {})
        t2_judge = JudgeEvalConfig(**t2_judge_raw)

        t3_judge_raw = data.get("evaluation", {}).get("tier_3_llm_judge", {})
        t3_judge = JudgeEvalConfig(**t3_judge_raw)

        global_invariant_ids = [
            inv.get("id", "")
            for inv in data.get("global_invariants", {}).get("invariants", [])
        ]

        return cls(
            benchmark_id=bench["id"],
            benchmark_name=bench["name"],
            benchmark_version=schema,
            fixtures=fixtures,
            tiers=tiers,
            tier_2_judge=t2_judge,
            tier_3_judge=t3_judge,
            global_invariant_ids=global_invariant_ids,
        )


def load_tier_cases(case_file_path: Path, tier_id: str) -> list:
    """Load case definitions from a tier YAML file."""
    with open(case_file_path) as f:
        data = yaml.safe_load(f)

    cases = data.get("cases", [])
    results: list = []

    if tier_id == "tier_1":
        for case_data in cases:
            results.append(Tier1Case(**case_data))
    elif tier_id == "tier_2":
        for case_data in cases:
            results.append(Tier2Case(**case_data))
    elif tier_id == "tier_3":
        for case_data in cases:
            results.append(Tier3Case(**case_data))

    return results
