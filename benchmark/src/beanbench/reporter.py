"""Self-contained HTML report generator for benchmark results."""

import json
from datetime import datetime

REPORT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BeanBench Report</title>
<style>
  :root {
    --bg: #0d1117;
    --fg: #c9d1d9;
    --border: #30363d;
    --link: #58a6ff;
    --green: #3fb950;
    --red: #f85149;
    --yellow: #d29922;
    --muted: #8b949e;
    --row-hover: #161b22;
    --header-bg: #161b22;
    --card-bg: #161b22;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--fg);
    padding: 24px;
    line-height: 1.5;
  }
  h1 { font-size: 1.5rem; margin-bottom: 4px; }
  h2 { font-size: 1.1rem; color: var(--muted); margin-bottom: 16px; font-weight: 400; }
  .controls { margin-bottom: 20px; display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-end; }
  .controls label { font-size: 0.8rem; color: var(--muted); display: block; margin-bottom: 2px; }
  .controls select, .controls input {
    background: var(--header-bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 0.85rem;
    min-width: 160px;
  }
  .summary-cards { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 24px; }
  .card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 18px;
    min-width: 140px;
  }
  .card .label { font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
  .card .value { font-size: 1.4rem; font-weight: 600; margin-top: 2px; }
  .card .sub { font-size: 0.75rem; color: var(--muted); margin-top: 2px; }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.85rem;
  }
  th, td {
    padding: 8px 12px;
    text-align: left;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  th {
    background: var(--header-bg);
    color: var(--muted);
    font-weight: 600;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    position: sticky;
    top: 0;
  }
  tr:hover { background: var(--row-hover); }
  .score-bar {
    display: inline-block;
    height: 8px;
    border-radius: 4px;
    background: var(--green);
    vertical-align: middle;
    margin-right: 6px;
  }
  .score-bar.low { background: var(--red); }
  .score-bar.mid { background: var(--yellow); }
  .violation-badge {
    display: inline-block;
    background: var(--red);
    color: #fff;
    font-size: 0.7rem;
    padding: 1px 6px;
    border-radius: 10px;
    font-weight: 600;
  }
  .violation-badge.clean {
    background: var(--green);
  }
  .case-detail {
    display: none;
    padding: 8px 12px;
    background: var(--row-hover);
    border-bottom: 1px solid var(--border);
    font-size: 0.82rem;
    color: var(--muted);
    max-height: 300px;
    overflow-y: auto;
  }
  .case-detail.open { display: block; }
  .case-row { cursor: pointer; }
  .footer { margin-top: 24px; font-size: 0.75rem; color: var(--muted); text-align: center; }
  @media (max-width: 768px) {
    body { padding: 12px; }
    table { font-size: 0.75rem; }
    th, td { padding: 6px 8px; }
  }
</style>
</head>
<body>

<h1>BeanBench Report</h1>
<h2>{{ report_title }}</h2>

<div class="controls">
  <div>
    <label for="model-filter">Filter by Model</label>
    <select id="model-filter" onchange="filterRows()">
      <option value="">All Models</option>
      {% for m in model_list %}<option value="{{ m }}">{{ m }}</option>{% endfor %}
    </select>
  </div>
  <div>
    <label for="model-input">Model / Base URL for new run</label>
    <input id="model-input" type="text" placeholder="model-name">
  </div>
  <div>
    <label for="baseurl-input">Base URL</label>
    <input id="baseurl-input" type="text" placeholder="https://api.openai.com/v1">
  </div>
</div>

<div id="cards" class="summary-cards"></div>

<table id="results-table">
  <thead>
    <tr>
      <th>Model</th>
      <th>Run ID</th>
      <th>Date</th>
      <th>Tier 1</th>
      <th>Tier 2</th>
      <th>Tier 3</th>
      <th>Total</th>
      <th>Violations</th>
    </tr>
  </thead>
  <tbody id="table-body"></tbody>
</table>

<div id="case-panel" style="margin-top: 16px; display: none">
  <h3 style="margin-bottom: 8px">Case Details</h3>
  <table>
    <thead>
      <tr>
        <th>Case</th><th>Score</th><th>Max</th><th>Passed</th>
        <th>Time</th><th>Trace</th><th>Errors / Judge</th>
      </tr>
    </thead>
    <tbody id="case-body"></tbody>
  </table>
</div>

<div class="footer">Generated {{ generated_at }} &mdash; BeanBench v1.0</div>

<script>
  const DATA = {{ data_json }};

  const MAX_SCORES = { "tier_1": 35, "tier_2": 30, "tier_3": 48 };
  const TOTAL_MAX = 113;

  function scorePct(score, max) {
    if (!max || max <= 0) return 0;
    return Math.round((score / max) * 100);
  }

  function scoreClass(score, max) {
    if (!max || max <= 0) return "";
    const pct = score / max;
    if (pct >= 0.8) return "score-bar";
    if (pct >= 0.5) return "score-bar mid";
    return "score-bar low";
  }

  function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })
      + " " + d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
  }

  function renderBar(score, max, label) {
    if (max <= 0) return `<span style="color:var(--muted)">--</span>`;
    const pct = scorePct(score, max);
    return `<span class="${scoreClass(score, max)}" style="width:${Math.max(2, pct * 0.6)}px"></span>${score} <span style="color:var(--muted);font-size:0.7rem">/ ${max}</span>`;
  }

  function renderViolations(count) {
    if (count > 0) return `<span class="violation-badge">${count}</span>`;
    return `<span class="violation-badge clean">0</span>`;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderTrace(details) {
    const traceId = details?.trace_id || "";
    const traceUrl = details?.trace_url || "";
    if (!traceId) return '<span style="color:var(--muted)">--</span>';

    const shortId = traceId.length > 12 ? `${traceId.slice(0, 12)}...` : traceId;
    const label = escapeHtml(shortId);
    const title = escapeHtml(traceId);
    if (traceUrl) {
      const href = escapeHtml(traceUrl);
      return `<a href="${href}" target="_blank" rel="noreferrer" title="${title}">
        ${label}
      </a>`;
    }
    return `<code title="${title}">${label}</code>`;
  }

  function filterRows() {
    const filterVal = document.getElementById("model-filter").value;
    const rows = document.getElementById("table-body").querySelectorAll("tr");
    rows.forEach(row => {
      if (!filterVal || row.dataset.model === filterVal) {
        row.style.display = "";
      } else {
        row.style.display = "none";
      }
    });
    updateCards();
  }

  function updateCards() {
    const filterVal = document.getElementById("model-filter").value;
    let filtered = DATA;
    if (filterVal) filtered = DATA.filter(r => r.model_id === filterVal);

    const latest = filtered.length > 0 ? filtered[0] : null;
    const uniqueModels = new Set(filtered.map(r => r.model_id)).size;

    document.getElementById("cards").innerHTML = `
      <div class="card"><div class="label">Total Runs</div><div class="value">${filtered.length}</div><div class="sub">${uniqueModels} model${uniqueModels !== 1 ? 's' : ''}</div></div>
      ${latest ? `
      <div class="card"><div class="label">Latest Total</div><div class="value">${latest.total_score} <span style="color:var(--muted);font-size:0.7rem">/ ${TOTAL_MAX}</span></div><div class="sub">${latest.model_id}</div></div>
      <div class="card"><div class="label">Latest T1</div><div class="value">${latest.tier_scores?.tier_1 ?? '--'}</div><div class="sub">/ ${MAX_SCORES.tier_1}</div></div>
      <div class="card"><div class="label">Latest T2</div><div class="value">${latest.tier_scores?.tier_2 ?? '--'}</div><div class="sub">/ ${MAX_SCORES.tier_2}</div></div>
      <div class="card"><div class="label">Latest T3</div><div class="value">${latest.tier_scores?.tier_3 ?? '--'}</div><div class="sub">/ ${MAX_SCORES.tier_3}</div></div>
      ` : '<div class="card"><div class="label">No Data</div><div class="value">--</div></div>'}
    `;
  }

  function showCaseDetails(runIdx) {
    const run = DATA[runIdx];
    if (!run || !run.case_results) return;

    const panel = document.getElementById("case-panel");
    const body = document.getElementById("case-body");

    let html = "";
    for (const cr of run.case_results || []) {
      const errors = cr.details?.errors || [];
      const judge = cr.details?.judge || {};
      const rt = cr.details?.response_time_ms;
      const rtStr = rt != null ? `${(rt / 1000).toFixed(1)}s` : '';
      const detailText = errors.length > 0
        ? errors.join("; ")
        : (judge.reason || judge.fatal_errors?.join(", ") || "");
      const escapedDetail = escapeHtml(detailText);

      html += `<tr>
        <td>${escapeHtml(cr.case_id)}</td>
        <td>${cr.score}</td>
        <td>${cr.max_score}</td>
        <td>${cr.passed ? '&#x2705;' : '&#x274C;'}</td>
        <td>${rtStr}</td>
        <td>${renderTrace(cr.details)}</td>
        <td
          style="max-width:400px;overflow:hidden;text-overflow:ellipsis"
          title="${escapedDetail}"
        >${escapeHtml(detailText.substring(0, 120))}</td>
      </tr>`;
    }

    body.innerHTML = html;
    panel.style.display = "block";
    panel.scrollIntoView({ behavior: "smooth" });
  }

  function renderTable() {
    const tbody = document.getElementById("table-body");
    let html = "";
    const modelSet = new Set();
    DATA.forEach((r, idx) => {
      modelSet.add(r.model_id);
      const tier1 = r.tier_scores?.tier_1 ?? 0;
      const tier2 = r.tier_scores?.tier_2 ?? 0;
      const tier3 = r.tier_scores?.tier_3 ?? 0;
      html += `<tr class="case-row" data-model="${r.model_id}" onclick="showCaseDetails(${idx})" title="Click for case details">
        <td style="font-weight:600">${r.model_id}</td>
        <td style="color:var(--muted);font-size:0.75rem">${(r._file || '').replace(/\\.[^.]+$/, '')}</td>
        <td style="color:var(--muted)">${fmtDate(r.completed_at)}</td>
        <td>${renderBar(tier1, MAX_SCORES.tier_1)}</td>
        <td>${renderBar(tier2, MAX_SCORES.tier_2)}</td>
        <td>${renderBar(tier3, MAX_SCORES.tier_3)}</td>
        <td><strong>${r.total_score}</strong> <span style="color:var(--muted);font-size:0.7rem">/ ${TOTAL_MAX}</span></td>
        <td>${renderViolations(r.global_invariant_violations)}</td>
      </tr>`;
    });
    tbody.innerHTML = html;

    const select = document.getElementById("model-filter");
    select.innerHTML = '<option value="">All Models</option>'
      + [...modelSet].sort().map(m => `<option value="${m}">${m}</option>`).join("");
  }

  renderTable();
  updateCards();
</script>
</body>
</html>"""


def generate_report(all_results: list[dict]) -> str:
    """Generate a self-contained HTML report from benchmark results."""
    from jinja2 import Template

    model_list = sorted(set(r.get("model_id", "?") for r in all_results))

    template = Template(REPORT_TEMPLATE)
    return template.render(
        report_title=f"Snapshot — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        model_list=model_list,
        data_json=json.dumps(all_results, default=str, indent=2),
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M UTC"),
    )
