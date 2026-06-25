"""Click CLI for BeanBench."""

import asyncio
import logging
import os
import sys
from pathlib import Path

import click

from .config import BenchmarkConfig
from .result_store import load_all_results
from .runner import RunConfig, run_benchmark

BENCHMARK_DIR = Path(__file__).resolve().parent.parent.parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("beanbench")


@click.group()
@click.version_option(version="0.1.0", prog_name="beanbench")
def cli():
    """BeanBench — benchmark Agent-Core as a trustworthy Beancount collaborator."""


@cli.command()
@click.option(
    "--model",
    required=True,
    help="Agent-core LLM model name",
    envvar="BEANBENCH_MODEL",
)
@click.option(
    "--base-url",
    help="Agent-core LLM base URL (e.g. https://api.deepseek.com)",
    envvar="BEANBENCH_BASE_URL",
)
@click.option(
    "--api-key",
    help="Agent-core LLM API key",
    envvar="BEANBENCH_API_KEY",
)
@click.option(
    "--judge-model",
    help="Judge LLM model name (default: gpt-4o)",
    envvar="BEANBENCH_JUDGE_MODEL",
)
@click.option(
    "--judge-base-url",
    help="Judge LLM base URL",
    envvar="BEANBENCH_JUDGE_BASE_URL",
)
@click.option(
    "--judge-api-key",
    help="Judge LLM API key",
    envvar="BEANBENCH_JUDGE_API_KEY",
)
@click.option(
    "--judge-tier1",
    is_flag=True,
    default=False,
    help="Use LLM judge for tier 1 instead of deterministic evaluation",
)
@click.option(
    "--langfuse-enabled",
    is_flag=True,
    default=False,
    help="Enable Langfuse tracing in agent-core",
)
@click.option(
    "--langfuse-public-key",
    help="Langfuse public key",
    envvar="LANGFUSE_PUBLIC_KEY",
)
@click.option(
    "--langfuse-secret-key",
    help="Langfuse secret key",
    envvar="LANGFUSE_SECRET_KEY",
)
@click.option(
    "--langfuse-base-url",
    help="Langfuse base URL",
    envvar="LANGFUSE_BASE_URL",
)
@click.option(
    "--tier",
    "tiers",
    multiple=True,
    type=click.Choice(["tier_1", "tier_2", "tier_3"]),
    help="Run only specific tier(s). Repeatable. Default: all tiers.",
)
@click.option(
    "--results-dir",
    default=str(BENCHMARK_DIR / "results"),
    help="Directory for result JSON files",
)
def run(**kwargs):
    """Run the benchmark suite against a local agent-core instance."""
    tiers = list(kwargs.pop("tiers", [])) or ["tier_1", "tier_2", "tier_3"]
    results_dir = Path(kwargs.pop("results_dir"))

    config_path = BENCHMARK_DIR / "requirements.yaml"
    if not config_path.exists():
        click.echo(f"ERROR: Benchmark config not found at {config_path}", err=True)
        sys.exit(1)

    config = BenchmarkConfig.from_yaml(config_path)

    api_key = kwargs.pop("api_key") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        click.echo(
            "WARNING: No agent API key provided. Set --api-key or BEANBENCH_API_KEY / OPENAI_API_KEY.",
            err=True,
        )

    run_config = RunConfig(
        model=kwargs.pop("model"),
        api_key=api_key,
        base_url=kwargs.pop("base_url"),
        judge_model=kwargs.pop("judge_model"),
        judge_api_key=kwargs.pop("judge_api_key"),
        judge_base_url=kwargs.pop("judge_base_url"),
        judge_tier1=kwargs.pop("judge_tier1", False),
        langfuse_enabled=kwargs.pop("langfuse_enabled", False),
        langfuse_public_key=kwargs.pop("langfuse_public_key"),
        langfuse_secret_key=kwargs.pop("langfuse_secret_key"),
        langfuse_base_url=kwargs.pop("langfuse_base_url"),
        tiers=tiers,
        results_dir=results_dir,
    )

    click.echo(f"BeanBench — {config.benchmark_name} v{config.benchmark_version}")
    click.echo(f"Model: {run_config.model}")
    click.echo(f"Tiers: {', '.join(tiers)}")
    click.echo(f"Results dir: {results_dir}")
    click.echo()

    result = asyncio.run(run_benchmark(config, run_config))

    click.echo()
    click.echo("=" * 60)
    click.echo("  BENCHMARK COMPLETE")
    click.echo("=" * 60)
    click.echo(f"  Model:           {result.model_id}")
    click.echo(f"  Judge model:     {result.judge_model}")
    click.echo(f"  Duration:        {(result.completed_at - result.started_at).total_seconds():.0f}s")
    click.echo(f"  Cases:           {len(result.case_results)}")
    for tier_key, score in sorted(result.tier_scores.items()):
        click.echo(f"  {tier_key}:        {score}")
    click.echo(f"  Total score:     {result.total_score}")
    click.echo(f"  Violations:      {result.global_invariant_violations}")
    click.echo()


@cli.command()
@click.option(
    "--results-dir",
    default=str(BENCHMARK_DIR / "results"),
    help="Directory containing result JSON files",
)
@click.option(
    "--output",
    default="benchmark-report.html",
    help="Output HTML file path",
)
def report(results_dir: str, output: str):
    """Generate a self-contained HTML report from benchmark results."""
    from .reporter import generate_report

    results_path = Path(results_dir)
    if not results_path.exists():
        click.echo(f"ERROR: Results directory not found: {results_path}", err=True)
        sys.exit(1)

    all_results = load_all_results(results_path)
    if not all_results:
        click.echo(f"No result files found in {results_path}", err=True)
        sys.exit(1)

    html = generate_report(all_results)
    output_path = Path(output)
    with open(output_path, "w") as f:
        f.write(html)

    click.echo(f"Report written to {output_path.resolve()}")
    click.echo(f"  Runs: {len(all_results)}")
    click.echo(f"  Models: {len(set(r.get('model_id', '?') for r in all_results))}")
