"""Main orchestrator — runs benchmark cases across tiers, fixtures, models."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    BenchmarkConfig,
    Tier1Case,
    Tier2Case,
    Tier3Case,
    load_tier_cases,
)
from .fixture_manager import fixture_context
from .llm_judge import judge_tier2, judge_tier3
from .requestor import (
    extract_beancount_block,
    run_multi_turn,
    run_single_turn,
)
from .result_store import (
    BenchmarkResult,
    CaseResult,
    get_git_commit,
    save_result,
)
from .tier1_evaluator import evaluate_tier1

logger = logging.getLogger(__name__)

BENCHMARK_DIR = Path(__file__).resolve().parent.parent.parent
RESULTS_DIR = BENCHMARK_DIR / "results"


class RunConfig:
    """Configuration for a single benchmark run."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        judge_model: str | None = None,
        judge_api_key: str | None = None,
        judge_base_url: str | None = None,
        judge_tier1: bool = False,
        tiers: list[str] | None = None,
        results_dir: Path | None = None,
    ):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.judge_model = judge_model
        self.judge_api_key = judge_api_key
        self.judge_base_url = judge_base_url or base_url
        self.judge_tier1 = judge_tier1
        self.tiers = tiers or ["tier_1", "tier_2", "tier_3"]
        self.results_dir = results_dir or RESULTS_DIR


async def run_benchmark(config: BenchmarkConfig, run_config: RunConfig) -> BenchmarkResult:
    """Execute the full benchmark suite against agent-core."""
    agent_env = {}
    if run_config.base_url:
        agent_env["OPENAI_BASE_URL"] = run_config.base_url

    agent_core_commit = get_git_commit(BENCHMARK_DIR.parent.parent)
    fixture_commit = get_git_commit(BENCHMARK_DIR / "fixtures")

    result = BenchmarkResult(
        benchmark_id=config.benchmark_id,
        model_id=run_config.model,
        agent_core_commit=agent_core_commit,
        fixture_commit=fixture_commit,
        started_at=datetime.now(timezone.utc),
        judge_model=run_config.judge_model or "gpt-4o",
    )

    total_cases = 0
    for tier_id in run_config.tiers:
        tier_meta = config.tiers.get(tier_id)
        if tier_meta is None:
            logger.warning("Tier %s not found in benchmark config, skipping", tier_id)
            continue

        case_file = BENCHMARK_DIR / tier_meta.case_file
        cases = load_tier_cases(case_file, tier_id)
        total_cases += len(cases)

    case_ix = 0
    for tier_id in run_config.tiers:
        tier_meta = config.tiers.get(tier_id)
        if tier_meta is None:
            continue

        case_file = BENCHMARK_DIR / tier_meta.case_file
        cases = load_tier_cases(case_file, tier_id)

        max_points = tier_meta.scoring.points_per_case

        for case in cases:
            case_ix += 1
            fixture_name = case.fixture
            fixture_config = config.fixtures.get(fixture_name)
            if fixture_config is None:
                logger.error("Fixture '%s' not found for case %s", fixture_name, case.id)
                result.case_results.append(CaseResult(
                    case_id=case.id, score=0, max_score=max_points,
                    passed=False, details={"error": f"Fixture '{fixture_name}' not found"}
                ))
                continue

            fixture_path = BENCHMARK_DIR / fixture_config.root
            if not fixture_path.exists():
                logger.error("Fixture path '%s' does not exist for case %s", fixture_path, case.id)
                result.case_results.append(CaseResult(
                    case_id=case.id, score=0, max_score=max_points,
                    passed=False, details={"error": f"Fixture path {fixture_path} does not exist"}
                ))
                continue

            fixture_main_path = fixture_path / fixture_config.entry_file
            fixture_main_content = ""
            if fixture_main_path.exists():
                fixture_main_content = fixture_main_path.read_text(encoding="utf-8")

            logger.info("[%d/%d] %s — %s (%s)", case_ix, total_cases, case.id, case.title, tier_id)

            try:
                if tier_id == "tier_1":
                    case_result = await _run_tier1(
                        case, run_config, agent_env, fixture_path, fixture_main_content,
                        benchmark_config=config if run_config.judge_tier1 else None,
                    )
                elif tier_id == "tier_2":
                    case_result = await _run_tier2(
                        case, run_config, config, agent_env, fixture_path
                    )
                elif tier_id == "tier_3":
                    case_result = await _run_tier3(
                        case, run_config, config, agent_env, fixture_path
                    )
                else:
                    case_result = CaseResult(
                        case_id=case.id, score=0, max_score=max_points,
                        passed=False, details={"error": f"Unknown tier: {tier_id}"}
                    )
            except Exception as e:
                logger.exception("Error running case %s", case.id)
                case_result = CaseResult(
                    case_id=case.id, score=0, max_score=max_points,
                    passed=False, details={"error": str(e)}
                )

            result.case_results.append(case_result)
            _save_incremental(result, run_config.results_dir)

    result.finalize(datetime.now(timezone.utc))
    saved_path = save_result(result, run_config.results_dir)
    logger.info("Results saved to %s", saved_path)

    return result


def _save_incremental(result: BenchmarkResult, results_dir: Path) -> None:
    """Save intermediate result after each case so Ctrl-C preserves progress."""
    result.finalize(datetime.now(timezone.utc))
    save_result(result, results_dir)


async def _run_tier1(
    case: Tier1Case,
    run_config: RunConfig,
    agent_env: dict[str, str],
    fixture_path: Path,
    fixture_main_content: str,
    benchmark_config: BenchmarkConfig | None = None,
) -> CaseResult:
    max_points = 1
    t_start = time.monotonic()
    with fixture_context(fixture_path, agent_env) as (port, _temp):
        resp = await run_single_turn(
            query=case.user_prompt,
            prior_messages=[],
            model=run_config.model,
            api_key=run_config.api_key,
            case_id=case.id,
            port=port,
        )
    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    resp_text = resp.get("response_text", "")
    details: dict = {"response": resp_text, "response_time_ms": elapsed_ms}

    if resp.get("error"):
        details["error"] = resp["error"]
        return CaseResult(case_id=case.id, score=0, max_score=max_points, passed=False, details=details)

    beancount_block = extract_beancount_block(resp_text)
    eval_result = evaluate_tier1(
        beancount_block or "",
        case.deterministic_assertions,
        fixture_content=fixture_main_content,
    )
    details["extracted_block"] = eval_result.extracted_block
    details["errors"] = eval_result.errors

    if benchmark_config is not None and not eval_result.passed:
        ref = case.reference_answer
        judge_result = judge_tier2(
            user_prompt=case.user_prompt,
            agent_response=resp_text,
            fixture_facts=[],
            reference_answer={
                "expected_disposition": ref.expected_disposition,
                "required_facts": ref.required_facts,
                "example_answer": ref.canonical_preview,
            },
            judge_anchors={
                "fatal_errors": [],
                "required_facts": [],
            },
            system_prompt=benchmark_config.tier_2_judge.system_prompt,
            judge_model=run_config.judge_model,
            judge_api_key=run_config.judge_api_key,
            judge_base_url=run_config.judge_base_url,
        )
        details["judge"] = judge_result
        score = min(judge_result.get("score", 0), max_points)
        return CaseResult(case_id=case.id, score=score, max_score=max_points, passed=score > 0, details=details)

    return CaseResult(
        case_id=case.id,
        score=eval_result.score,
        max_score=max_points,
        passed=eval_result.passed,
        details=details,
    )


async def _run_tier2(
    case: Tier2Case,
    run_config: RunConfig,
    benchmark_config: BenchmarkConfig,
    agent_env: dict[str, str],
    fixture_path: Path,
) -> CaseResult:
    max_points = 2
    t_start = time.monotonic()
    with fixture_context(fixture_path, agent_env) as (port, _temp):
        resp = await run_single_turn(
            query=case.user_prompt,
            prior_messages=[],
            model=run_config.model,
            api_key=run_config.api_key,
            case_id=case.id,
            port=port,
        )
    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    resp_text = resp.get("response_text", "")
    details: dict = {"response": resp_text, "response_time_ms": elapsed_ms}
    if resp.get("error"):
        details["error"] = resp["error"]
        return CaseResult(case_id=case.id, score=0, max_score=max_points, passed=False, details=details)

    ref = case.reference_answer
    judge_result = judge_tier2(
        user_prompt=case.user_prompt,
        agent_response=resp_text,
        fixture_facts=case.fixture_facts,
        reference_answer={
            "expected_disposition": ref.expected_disposition,
            "required_facts": ref.required_facts,
            "example_answer": ref.example_answer,
        },
        judge_anchors={
            "fatal_errors": case.judge_anchors.fatal_errors,
            "required_facts": case.judge_anchors.required_facts,
        },
        system_prompt=benchmark_config.tier_2_judge.system_prompt,
        judge_model=run_config.judge_model,
        judge_api_key=run_config.judge_api_key,
        judge_base_url=run_config.judge_base_url,
    )

    details["judge"] = judge_result
    score = min(judge_result.get("score", 0), max_points)
    return CaseResult(
        case_id=case.id, score=score, max_score=max_points,
        passed=score > 0, details=details,
    )


async def _run_tier3(
    case: Tier3Case,
    run_config: RunConfig,
    benchmark_config: BenchmarkConfig,
    agent_env: dict[str, str],
    fixture_path: Path,
) -> CaseResult:
    max_points = 4
    turns = [{"role": t.role, "content": t.content} for t in case.turns]
    t_start = time.monotonic()
    with fixture_context(fixture_path, agent_env) as (port, _temp):
        resp = await run_multi_turn(
            turns=turns,
            model=run_config.model,
            api_key=run_config.api_key,
            case_id=case.id,
            port=port,
        )
    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    resp_text = resp.get("response_text", "")
    details: dict = {"response": resp_text, "response_time_ms": elapsed_ms}
    if resp.get("error"):
        details["error"] = resp["error"]
        return CaseResult(case_id=case.id, score=0, max_score=max_points, passed=False, details=details)

    ref = case.final_reference_answer
    judge_result = judge_tier3(
        conversation_turns=turns,
        agent_final_response=resp_text,
        reference_progress=case.reference_progress,
        final_reference_answer={
            "expected_disposition": ref.expected_disposition,
            "canonical_preview": ref.canonical_preview,
            "required_facts": ref.required_facts,
        },
        judge_anchors={
            "fatal_errors": case.judge_anchors.fatal_errors,
            "required_facts": case.judge_anchors.required_facts,
        },
        system_prompt=benchmark_config.tier_3_judge.system_prompt,
        judge_model=run_config.judge_model,
        judge_api_key=run_config.judge_api_key,
        judge_base_url=run_config.judge_base_url,
    )

    details["judge"] = judge_result
    score = min(judge_result.get("score", 0), max_points)
    return CaseResult(
        case_id=case.id, score=score, max_score=max_points,
        passed=score > 0, details=details,
    )
