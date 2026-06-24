"""JSON result persistence for benchmark runs."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path


class CaseResult:
    """Result of a single benchmark case."""

    def __init__(
        self,
        case_id: str,
        score: int,
        max_score: int,
        passed: bool = True,
        details: dict | None = None,
    ):
        self.case_id = case_id
        self.score = score
        self.max_score = max_score
        self.passed = passed
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "score": self.score,
            "max_score": self.max_score,
            "passed": self.passed,
            "details": self.details,
        }


class BenchmarkResult:
    """A complete benchmark run result."""

    def __init__(
        self,
        benchmark_id: str,
        model_id: str,
        agent_core_commit: str,
        fixture_commit: str,
        started_at: datetime,
        judge_model: str,
    ):
        self.benchmark_id = benchmark_id
        self.model_id = model_id
        self.model_version = ""
        self.agent_core_commit = agent_core_commit
        self.fixture_commit = fixture_commit
        self.started_at = started_at
        self.completed_at: datetime | None = None
        self.tier_scores: dict[str, int] = {}
        self.total_score: int = 0
        self.global_invariant_violations: int = 0
        self.case_results: list[CaseResult] = []
        self.judge_model = judge_model
        self.judge_prompt_version = "1.0"

    def finalize(self, completed_at: datetime):
        self.completed_at = completed_at
        tier_totals: dict[str, int] = {}
        total = 0
        for cr in self.case_results:
            tier_id = cr.case_id.split("-")[0].lower()
            if tier_id.startswith("t1"):
                tier_key = "tier_1"
            elif tier_id.startswith("t2"):
                tier_key = "tier_2"
            elif tier_id.startswith("t3"):
                tier_key = "tier_3"
            else:
                tier_key = tier_id
            tier_totals[tier_key] = tier_totals.get(tier_key, 0) + cr.score
            total += cr.score
        self.tier_scores = tier_totals
        self.total_score = total

    def to_dict(self) -> dict:
        return {
            "benchmark_id": self.benchmark_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "agent_core_commit": self.agent_core_commit,
            "fixture_commit": self.fixture_commit,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "tier_scores": self.tier_scores,
            "total_score": self.total_score,
            "global_invariant_violations": self.global_invariant_violations,
            "case_results": [cr.to_dict() for cr in self.case_results],
            "judge_model": self.judge_model,
            "judge_prompt_version": self.judge_prompt_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BenchmarkResult":
        started = data.get("started_at")
        started_dt = datetime.fromisoformat(started) if started else datetime.now(timezone.utc)

        result = cls(
            benchmark_id=data["benchmark_id"],
            model_id=data["model_id"],
            agent_core_commit=data.get("agent_core_commit", ""),
            fixture_commit=data.get("fixture_commit", ""),
            started_at=started_dt,
            judge_model=data.get("judge_model", ""),
        )
        result.model_version = data.get("model_version", "")
        result.tier_scores = data.get("tier_scores", {})
        result.total_score = data.get("total_score", 0)
        result.global_invariant_violations = data.get("global_invariant_violations", 0)
        result.judge_prompt_version = data.get("judge_prompt_version", "1.0")

        completed = data.get("completed_at")
        if completed:
            result.completed_at = datetime.fromisoformat(completed)

        for cr_data in data.get("case_results", []):
            result.case_results.append(CaseResult(
                case_id=cr_data["case_id"],
                score=cr_data["score"],
                max_score=cr_data["max_score"],
                passed=cr_data.get("passed", True),
                details=cr_data.get("details", {}),
            ))

        return result


def get_git_commit(path: Path) -> str:
    """Get the current git commit hash for a directory."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def save_result(result: BenchmarkResult, results_dir: Path) -> Path:
    """Save a benchmark result to the results directory."""
    model_dir = results_dir / result.model_id
    model_dir.mkdir(parents=True, exist_ok=True)

    ts = result.started_at.strftime("%Y%m%dT%H%M%SZ") if result.started_at else "unknown"
    filename = f"{ts}.json"
    filepath = model_dir / filename

    with open(filepath, "w") as f:
        json.dump(result.to_dict(), f, indent=2, default=str)

    return filepath


def load_all_results(results_dir: Path) -> list[dict]:
    """Load all JSON result files from the results directory."""
    all_results: list[dict] = []
    if not results_dir.exists():
        return all_results

    for model_dir in results_dir.iterdir():
        if not model_dir.is_dir():
            continue
        for result_file in sorted(model_dir.glob("*.json")):
            try:
                with open(result_file) as f:
                    data = json.load(f)
                data["_file"] = str(result_file.relative_to(results_dir))
                all_results.append(data)
            except Exception:
                continue

    all_results.sort(key=lambda r: r.get("completed_at", ""), reverse=True)
    return all_results
