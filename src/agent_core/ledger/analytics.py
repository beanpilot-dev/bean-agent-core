"""Monthly financial analytics: run the report manifest against query templates."""

import csv
import io
import json
import logging
import os
import re
import subprocess
from datetime import datetime

from . import _beancount as bc

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "query_templates")

TEXT_COLUMNS = {"account", "date", "narration", "payee", "filename", "tags", "links"}


# ---------------------------------------------------------------------------
# Monthly report manifest
#
# Each entry describes one query to run as part of the monthly report.
# Keys:
#   id              - key in the output JSON (must match what report.py expects)
#   title           - human-readable label
#   type            - "table" | "scalar"
#   template        - filename stem in query_templates/ (without .bql)
#   bql             - raw BQL override (used instead of template when set)
#   account_pattern - substituted as {account_pattern} in the template
#   date_range      - "current" | "previous" | "none"
#   limit           - substituted as {limit} (default 10)
#   negate          - if True, flip sign of all numeric result values
#   rename_col      - dict mapping old column name → new column name in rows
# ---------------------------------------------------------------------------

MONTHLY_REPORT_MANIFEST = [
    # ── Current month ────────────────────────────────────────────────────────
    {
        "id": "income",
        "title": "本月收入明细",
        "type": "table",
        "template": "spending_breakdown",
        "account_pattern": "^Income:",
        "date_range": "current",
        "negate": True,
    },
    {
        "id": "expenses",
        "title": "本月支出明细",
        "type": "table",
        "template": "spending_breakdown",
        "account_pattern": "^Expenses:",
        "date_range": "current",
    },
    {
        "id": "fixed_purchases",
        "title": "本月固定资产购入",
        "type": "table",
        "template": "spending_breakdown",
        "account_pattern": "^Assets:Fixed:",
        "date_range": "current",
    },
    {
        "id": "top_outflows",
        "title": "本月最大支出 Top 5",
        "type": "table",
        "template": "large_transactions",
        "account_pattern": "^(Expenses:|Assets:Fixed:)",
        "date_range": "current",
        "limit": 5,
    },
    {
        "id": "top_inflows",
        "title": "本月最大收入 Top 5",
        "type": "table",
        "date_range": "current",
        # Income accounts are negative; ORDER BY position ASC = largest income first
        "bql": (
            "SELECT date, narration, account, neg(position) AS amount "
            "WHERE account ~ '^Income:' "
            "  AND date >= {start} AND date < {end} "
            "ORDER BY position ASC LIMIT 5"
        ),
    },
    # ── Previous month (for MoM comparison) ─────────────────────────────────
    {
        "id": "prev_income",
        "title": "上月总收入",
        "type": "scalar",
        "template": "period_total",
        "account_pattern": "^Income:",
        "date_range": "previous",
        "negate": True,
        "rename_col": {"total": "prev_income"},
    },
    {
        "id": "prev_expenses",
        "title": "上月总支出",
        "type": "scalar",
        "template": "period_total",
        "account_pattern": "^Expenses:",
        "date_range": "previous",
        "rename_col": {"total": "prev_expenses"},
    },
    {
        "id": "prev_fixed",
        "title": "上月固定资产购入",
        "type": "scalar",
        "template": "period_total",
        "account_pattern": "^Assets:Fixed:",
        "date_range": "previous",
        "rename_col": {"total": "prev_fixed"},
    },
    {
        "id": "prev_expenses_detail",
        "title": "上月支出明细",
        "type": "table",
        "template": "spending_breakdown",
        "account_pattern": "^Expenses:",
        "date_range": "previous",
    },
    # ── Balance-sheet snapshots (no date filter) ─────────────────────────────
    {
        "id": "liquid_snapshot",
        "title": "流动资产余额",
        "type": "table",
        "template": "account_snapshot",
        "account_pattern": "^Assets:Liquid:",
    },
    {
        "id": "liabilities",
        "title": "负债余额",
        "type": "table",
        "template": "account_snapshot",
        "account_pattern": "^Liabilities:",
    },
    {
        "id": "fixed_snapshot",
        "title": "固定资产累计",
        "type": "table",
        "template": "account_snapshot",
        "account_pattern": "^Assets:Fixed:",
    },
    {
        "id": "net_worth",
        "title": "净资产快照",
        "type": "scalar",
        "template": "account_total",
        "account_pattern": "^(Assets:|Liabilities:)",
        "rename_col": {"total": "net_worth"},
    },
]


# ---------------------------------------------------------------------------
# Template loading
# ---------------------------------------------------------------------------

def _load_templates() -> dict[str, str]:
    """Load all .bql files from query_templates/ and return {stem: bql_body}."""
    templates: dict[str, str] = {}
    for fname in os.listdir(_TEMPLATES_DIR):
        if not fname.endswith(".bql"):
            continue
        stem = fname[:-4]
        path = os.path.join(_TEMPLATES_DIR, fname)
        with open(path) as f:
            lines = []
            for line in f:
                if not re.match(r"^--\s*\w+:", line):
                    lines.append(line)
            templates[stem] = "".join(lines).strip()
    return templates


def load_template(template_name: str) -> str:
    """Load a single template by name. Raises FileNotFoundError if not found."""
    path = os.path.join(_TEMPLATES_DIR, f"{template_name}.bql")
    with open(path) as f:
        lines = [line for line in f if not re.match(r"^--\s*\w+:", line)]
    return "".join(lines).strip()


def list_templates() -> list[str]:
    """Return sorted list of available template names."""
    return sorted(
        f[:-4] for f in os.listdir(_TEMPLATES_DIR) if f.endswith(".bql")
    )


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

def _compute_dates(year: int, month: int) -> dict[str, str]:
    month_start = f"{year}-{month:02d}-01"
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    month_end = f"{ny}-{nm:02d}-01"
    py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
    prev_start = f"{py}-{pm:02d}-01"
    return {
        "month_start": month_start,
        "month_end": month_end,
        "prev_start": prev_start,
        "prev_end": month_start,
    }


def _run_bql(workspace: str, main_file: str, bql: str) -> tuple[str | None, str | None]:
    proc = subprocess.run(
        [bc._bean_bin(workspace, "bean-query"), "-f", "csv", main_file, bql],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None, proc.stderr.strip()
    return proc.stdout.replace("\r", "").strip(), None


def _parse_csv(csv_text: str) -> tuple[list[str], list[list[str]]]:
    if not csv_text:
        return [], []
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return [], []
    headers = [h.strip() for h in rows[0]]
    data = [[c.strip() for c in row] for row in rows[1:]]
    return headers, data


def _parse_amount(raw: str) -> dict:
    raw = raw.strip()
    if not raw:
        return {"number": 0, "currency": "CNY", "amounts": [], "raw": ""}
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    amounts = []
    for part in parts:
        m = re.match(r"^(-?[\d,]+\.?\d*)\s+(\S+)$", part)
        if m:
            amounts.append({"number": float(m.group(1).replace(",", "")), "currency": m.group(2)})
    result = {"raw": raw, "amounts": amounts}
    if len(amounts) == 1:
        result["number"] = amounts[0]["number"]
        result["currency"] = amounts[0]["currency"]
    return result


def _negate_rows(rows: list[dict]) -> None:
    """Flip sign of all numeric amount fields in place."""
    for row in rows:
        for val in row.values():
            if isinstance(val, dict) and "number" in val:
                val["number"] = -val["number"]
                for amt in val.get("amounts", []):
                    amt["number"] = -amt["number"]
                if val.get("raw"):
                    val["raw"] = re.sub(
                        r"(-?[\d,]+\.?\d*)",
                        lambda m: str(-float(m.group(1).replace(",", ""))),
                        val["raw"],
                        count=1,
                    )


def _rename_col(rows: list[dict], headers: list[str], mapping: dict) -> list[str]:
    """Rename columns in rows and headers in place. Returns updated headers."""
    for old, new in mapping.items():
        for row in rows:
            if old in row:
                row[new] = row.pop(old)
    return [mapping.get(h, h) for h in headers]


def _run_manifest_entry(
    workspace: str,
    main_file: str,
    entry: dict,
    dates: dict[str, str],
    templates: dict[str, str],
) -> dict:
    """Execute one manifest entry and return the result dict."""
    # Build BQL
    if "bql" in entry:
        bql = entry["bql"]
    else:
        bql = templates[entry["template"]]

    # Substitute date placeholders
    date_range = entry.get("date_range", "none")
    if date_range == "current":
        bql = bql.replace("{start}", dates["month_start"]).replace("{end}", dates["month_end"])
    elif date_range == "previous":
        bql = bql.replace("{start}", dates["prev_start"]).replace("{end}", dates["prev_end"])

    # Substitute other params
    if "account_pattern" in entry:
        bql = bql.replace("{account_pattern}", entry["account_pattern"])
    limit = entry.get("limit", 10)
    bql = bql.replace("{limit}", str(limit))

    # Run
    csv_output, error = _run_bql(workspace, main_file, bql)
    if error:
        return {
            "title": entry.get("title", entry["id"]),
            "type": entry.get("type", "table"),
            "error": error,
        }

    headers, raw_rows = _parse_csv(csv_output)
    parsed_rows = []
    for row in raw_rows:
        parsed_row = {}
        for i, header in enumerate(headers):
            val = row[i] if i < len(row) else ""
            parsed_row[header] = val if header in TEXT_COLUMNS else _parse_amount(val)
        parsed_rows.append(parsed_row)

    if entry.get("negate"):
        _negate_rows(parsed_rows)

    if entry.get("rename_col"):
        headers = _rename_col(parsed_rows, headers, entry["rename_col"])

    return {
        "title": entry.get("title", entry["id"]),
        "type": entry.get("type", "table"),
        "period": date_range,
        "headers": headers,
        "rows": parsed_rows,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(
    workspace: str, year: int = 0, month: int = 0, entry_path: str = "data/main.beancount"
) -> str:
    """Run the monthly report manifest and return a JSON string."""
    now = datetime.now()
    y = year if year > 0 else now.year
    m = month if month > 0 else now.month

    main_file = os.path.join(workspace, entry_path)
    dates = _compute_dates(y, m)
    templates = _load_templates()

    elapsed = (y - 2026) * 12 + m
    result = {
        "meta": {
            "year": y, "month": m,
            "month_label": f"{y}-{m:02d}",
            "period": f"{dates['month_start']} to {dates['month_end']}",
            "generated": now.isoformat(),
        },
        "goal": {
            "target_cny": 300000, "deadline": "2028-12",
            "total_months": 36, "months_elapsed": elapsed,
            "months_remaining": max(0, 36 - elapsed),
        },
        "queries": {},
    }

    for entry in MONTHLY_REPORT_MANIFEST:
        result["queries"][entry["id"]] = _run_manifest_entry(
            workspace, main_file, entry, dates, templates
        )

    return json.dumps(result, ensure_ascii=False, indent=2)
