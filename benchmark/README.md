# BeanBench

Benchmark framework for evaluating Agent-Core as a trustworthy Beancount collaborator. 62 cases across 3 tiers, 5 synthetic fixture ledgers, deterministic and LLM-judge scoring.

## Quick Start

```bash
cd agent-core/benchmark
pip install -e .

# Run all tiers
python -m beanbench run \
  --model deepseek-v4-flash \
  --base-url https://api.deepseek.com \
  --api-key sk-...

# Run one case while iterating on a focused behavior
python -m beanbench run \
  --model deepseek-v4-flash \
  --base-url https://api.deepseek.com \
  --api-key sk-... \
  --case-id T1-07

# Generate HTML report from saved results
python -m beanbench report
```

## CLI Reference

### `run` — Execute benchmarks

```
python -m beanbench run [OPTIONS]
```

| Option | Env var | Description |
|--------|---------|-------------|
| `--model` (required) | `BEANBENCH_MODEL` | Agent-core LLM model |
| `--base-url` | `BEANBENCH_BASE_URL` | LLM API base URL |
| `--api-key` | `BEANBENCH_API_KEY` | LLM API key |
| `--judge-model` | `BEANBENCH_JUDGE_MODEL` | Judge LLM model (default: `gpt-4o`) |
| `--judge-base-url` | `BEANBENCH_JUDGE_BASE_URL` | Judge LLM base URL (defaults to `--base-url`) |
| `--judge-api-key` | `BEANBENCH_JUDGE_API_KEY` | Judge LLM API key |
| `--judge-tier1` | — | Use LLM judge for tier 1 (instead of deterministic) |
| `--tier tier_1` | — | Run specific tiers (repeatable; default: all) |
| `--case-id T1-07` | — | Run one explicit case ID; respects `--tier` when provided |
| `--langfuse-enabled` | — | Enable Langfuse tracing in agent-core |
| `--langfuse-public-key` | `LANGFUSE_PUBLIC_KEY` | Langfuse public key |
| `--langfuse-secret-key` | `LANGFUSE_SECRET_KEY` | Langfuse secret key |
| `--langfuse-base-url` | `LANGFUSE_BASE_URL` | Langfuse base URL |
| `--results-dir` | — | Output directory (default: `results/`) |

### `report` — Generate HTML report

```
python -m beanbench report [--results-dir results/] [--output benchmark-report.html]
```

## How It Works

1. The runner reads `requirements.yaml` and tier case files from `cases/`.
2. For each case, the corresponding fixture is copied to a temp directory and initialized as a git repo.
3. Agent-core is started in local mode (`AGENT_MODE=local`) pointing at the temp fixture.
4. `POST /agent/chat` requests are sent via SSE for each case turn.
5. **Tier 1**: Extracted Beancount code blocks are parsed and validated against deterministic assertions (account, date, currency, value, tags, links, metadata).
6. **Tier 2 & 3**: A separate LLM judge scores accounting correctness, stewardship, and multi-turn state preservation.
7. Results persist incrementally to `results/{model_id}/{timestamp}.json`.

## Tiers

| Tier | Name | Cases | Points | Evaluator |
|------|------|-------|--------|-----------|
| 1 | CLERK Behavior Check | 35 | 35 | Deterministic (Beancount parser) |
| 2 | Ledger Literacy | 15 | 30 | LLM judge (score 0–2 per case) |
| 3 | Ledger Stewardship | 12 | 48 | LLM judge (score 0–4 per case) |

## Fixtures

| Fixture | Description |
|---------|-------------|
| `basic-us-v1` | Standard US personal ledger with 40+ accounts |
| `multi-currency-us-v1` | USD/EUR ledger with currency exchange |
| `investments-us-v1` | ETF purchases, dividends, sales with cost basis |
| `imports-us-v1` | CSV import with duplicate detection |
| `legacy-split-us-v1` | Read-only legacy files with current-period adjustments |

## Results Format

```json
{
  "benchmark_id": "beanbench-v1",
  "model_id": "deepseek-v4-flash",
  "tier_scores": {"tier_1": 28, "tier_2": 18, "tier_3": 32},
  "total_score": 78,
  "global_invariant_violations": 0,
  "case_results": [...]
}
```
