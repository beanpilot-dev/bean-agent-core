"""Generate HTML monthly report from query JSON data."""

import json
import logging
import os
from dataclasses import dataclass

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")


# ── Data helpers ─────────────────────────────────────────────────────────────

def _get_cny(amount_obj: dict) -> float:
    if not amount_obj:
        return 0.0
    if "number" in amount_obj and amount_obj.get("currency") == "CNY":
        return amount_obj["number"]
    for amt in amount_obj.get("amounts", []):
        if amt.get("currency") == "CNY":
            return amt["number"]
    return 0.0


def _fmt(num: float) -> str:
    return f"-¥{abs(num):,.2f}" if num < 0 else f"¥{num:,.2f}"


def _format_amt(amount_obj: dict) -> str:
    if not amount_obj:
        return "—"
    parts = [f"{a['number']:,.2f} {a['currency']}" for a in amount_obj.get("amounts", [])]
    if parts:
        return " + ".join(parts)
    if "number" in amount_obj:
        return f"{amount_obj['number']:,.2f} {amount_obj.get('currency', '')}".strip()
    return amount_obj.get("raw", "") or "—"


def _short_account(account: str) -> str:
    parts = account.split(":")
    return ":".join(parts[1:]) if len(parts) > 2 else (parts[-1] if len(parts) > 1 else account)


def _sum_cny(rows: list, field: str = "amount") -> float:
    return sum(_get_cny(row.get(field, {})) for row in rows)


def _query_rows(queries: dict, name: str) -> list:
    return queries.get(name, {}).get("rows", [])


# ── Metrics ───────────────────────────────────────────────────────────────────

@dataclass
class Metrics:
    income_cny: float
    expenses_cny: float
    fixed_cny: float
    total_outflow: float
    savings: float
    savings_rate: float
    liquid_cny: float
    liabilities_cny: float
    net_liquid: float
    net_worth: float
    fixed_total: float
    goal_target: float
    goal_pct: float
    elapsed_pct: float
    monthly_needed: float
    prev_income: float
    prev_expenses: float
    prev_fixed: float
    income_change: float
    expense_change: float
    fixed_change: float


def _compute_metrics(data: dict) -> Metrics:
    goal = data["goal"]
    q = data["queries"]

    income_cny   = _sum_cny(_query_rows(q, "income"))
    expenses_cny = _sum_cny(_query_rows(q, "expenses"))
    fixed_cny    = _sum_cny(_query_rows(q, "fixed_purchases"))
    total_outflow = expenses_cny + fixed_cny
    savings = income_cny - total_outflow
    savings_rate = (savings / income_cny * 100) if income_cny > 0 else 0.0

    liquid_cny      = _sum_cny(_query_rows(q, "liquid_snapshot"), "total")
    liabilities_cny = _sum_cny(_query_rows(q, "liabilities"), "total")
    fixed_total     = _sum_cny(_query_rows(q, "fixed_snapshot"), "total")
    net_liquid = liquid_cny + liabilities_cny
    net_worth  = net_liquid + fixed_total

    nw_rows = _query_rows(q, "net_worth")
    if nw_rows:
        net_worth = _get_cny(nw_rows[0].get("net_worth", {}))

    goal_target  = goal["target_cny"]
    goal_pct     = min(100.0, max(0.0, net_liquid / goal_target * 100)) if goal_target else 0.0
    elapsed_pct  = goal["months_elapsed"] / goal["total_months"] * 100 if goal["total_months"] else 0.0
    monthly_needed = (
        (goal_target - net_liquid) / goal["months_remaining"]
        if goal["months_remaining"] > 0 else 0.0
    )

    prev_income_rows  = _query_rows(q, "prev_income")
    prev_expense_rows = _query_rows(q, "prev_expenses")
    prev_fixed_rows   = _query_rows(q, "prev_fixed")
    prev_income   = _get_cny(prev_income_rows[0].get("prev_income", {}))   if prev_income_rows  else 0.0
    prev_expenses = _get_cny(prev_expense_rows[0].get("prev_expenses", {})) if prev_expense_rows else 0.0
    prev_fixed    = _get_cny(prev_fixed_rows[0].get("prev_fixed", {}))      if prev_fixed_rows   else 0.0

    return Metrics(
        income_cny=income_cny, expenses_cny=expenses_cny, fixed_cny=fixed_cny,
        total_outflow=total_outflow, savings=savings, savings_rate=savings_rate,
        liquid_cny=liquid_cny, liabilities_cny=liabilities_cny,
        net_liquid=net_liquid, net_worth=net_worth, fixed_total=fixed_total,
        goal_target=goal_target, goal_pct=goal_pct, elapsed_pct=elapsed_pct,
        monthly_needed=monthly_needed,
        prev_income=prev_income, prev_expenses=prev_expenses, prev_fixed=prev_fixed,
        income_change=income_cny - prev_income,
        expense_change=expenses_cny - prev_expenses,
        fixed_change=fixed_cny - prev_fixed,
    )


# ── Jinja2 filters ────────────────────────────────────────────────────────────

def _filter_arrow_delta(val: float, invert: bool = False) -> str:
    if val > 0:
        klass = "green" if not invert else "red"
        return f'<span class="{klass}">▲ {_fmt(abs(val))}</span>'
    if val < 0:
        klass = "red" if not invert else "green"
        return f'<span class="{klass}">▼ {_fmt(abs(val))}</span>'
    return '<span class="muted">— unchanged</span>'


def _build_jinja_env(templates_dir: str) -> Environment:
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=False)
    env.filters["fmt"]           = _fmt
    env.filters["amt"]           = _format_amt
    env.filters["cny"]           = _get_cny
    env.filters["short_account"] = _short_account
    env.filters["arrow_delta"]   = _filter_arrow_delta
    return env


def _build_context(data: dict) -> dict:
    m = _compute_metrics(data)
    q_raw = data["queries"]

    class Q:
        def __init__(self, d):
            self.__dict__.update(d)
            if "rows" not in self.__dict__:
                self.rows = []

    q = type("Queries", (), {k: Q(v) for k, v in q_raw.items()})()

    prev_expense_by_account = {
        row.get("account", ""): _get_cny(row.get("amount", {}))
        for row in _query_rows(q_raw, "prev_expenses_detail")
    }

    items = [
        (_short_account(row.get("account", "")), _get_cny(row.get("amount", {})))
        for row in _query_rows(q_raw, "expenses")
        if _get_cny(row.get("amount", {})) > 0
    ] + [
        (_short_account(row.get("account", "")) + " ★", _get_cny(row.get("amount", {})))
        for row in _query_rows(q_raw, "fixed_purchases")
        if _get_cny(row.get("amount", {})) > 0
    ]
    items.sort(key=lambda x: -x[1])
    max_val = max((v for _, v in items), default=1.0)
    outflow_bars = [
        (label, value, value / max_val * 100, value / m.total_outflow * 100 if m.total_outflow else 0)
        for label, value in items
    ]

    return dict(
        meta=data["meta"], goal=data["goal"], m=m, q=q,
        prev_expense_by_account=prev_expense_by_account,
        outflow_bars=outflow_bars,
        progress_blocks=max(0, min(40, int(m.goal_pct / 2.5))),
    )


# ── Public API ────────────────────────────────────────────────────────────────

def run(workspace: str, query_json: str) -> str:
    """Render the HTML report from query JSON. Returns the output file path."""
    data = json.loads(query_json)
    label = data["meta"]["month_label"]
    output_dir = os.path.join(workspace, "reports")
    os.makedirs(output_dir, exist_ok=True)

    templates_dir = os.path.normpath(_TEMPLATES_DIR)
    env = _build_jinja_env(templates_dir)
    ctx = _build_context(data)

    html_path = os.path.join(output_dir, f"{label}.html")
    env.get_template("report.html.j2").stream(ctx).dump(html_path, encoding="utf-8")
    logger.info("Report written to %s", html_path)
    return html_path
