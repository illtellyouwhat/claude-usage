"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import json
import os
import sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime

DB_PATH = Path.home() / ".claude" / "usage.db"
RATES_OVERRIDE_PATH = Path.home() / ".claude" / "claude_usage_rates.json"

DEFAULT_PRICING = {
    "claude-opus-4-7":   {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-5":   {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-sonnet-4-7": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-7":  {"input": 1.00, "output":  5.00, "cache_write": 1.25, "cache_read": 0.10},
    "claude-haiku-4-6":  {"input": 1.00, "output":  5.00, "cache_write": 1.25, "cache_read": 0.10},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "cache_write": 1.25, "cache_read": 0.10},
}


def load_pricing():
    pricing = dict(DEFAULT_PRICING)
    if RATES_OVERRIDE_PATH.exists():
        try:
            overrides = json.loads(RATES_OVERRIDE_PATH.read_text())
            pricing.update(overrides)
        except Exception:
            pass
    return pricing


def get_dashboard_data(db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ── All models (for filter UI) ────────────────────────────────────────────
    model_rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model
        FROM turns
        GROUP BY model
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """).fetchall()
    all_models = [r["model"] for r in model_rows]

    # ── Daily per-model, ALL history (client filters by range) ────────────────
    daily_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)   as day,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            COUNT(*)                   as turns
        FROM turns
        GROUP BY day, model
        ORDER BY day, model
    """).fetchall()

    daily_by_model = [{
        "day":            r["day"],
        "model":          r["model"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
    } for r in daily_rows]

    # ── Hourly per-day per-model (kept for potential future use) ──────────────
    hourly_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)                  as day,
            CAST(substr(timestamp, 12, 2) AS INTEGER) as hour,
            COALESCE(model, 'unknown')                as model,
            SUM(output_tokens)                        as output,
            COUNT(*)                                  as turns
        FROM turns
        WHERE timestamp IS NOT NULL AND length(timestamp) >= 13
        GROUP BY day, hour, model
        ORDER BY day, hour, model
    """).fetchall()

    hourly_by_model = [{
        "day":    r["day"],
        "hour":   r["hour"] if r["hour"] is not None else 0,
        "model":  r["model"],
        "output": r["output"] or 0,
        "turns":  r["turns"] or 0,
    } for r in hourly_rows]

    # ── All sessions (client filters by range and model) ──────────────────────
    session_rows = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, model, turn_count,
            git_branch
        FROM sessions
        ORDER BY last_timestamp DESC
    """).fetchall()

    sessions_all = []
    for r in session_rows:
        try:
            t1 = datetime.fromisoformat(r["first_timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(r["last_timestamp"].replace("Z", "+00:00"))
            duration_min = round((t2 - t1).total_seconds() / 60, 1)
        except Exception:
            duration_min = 0
        sessions_all.append({
            "session_id":    r["session_id"][:8],
            "project":       r["project_name"] or "unknown",
            "branch":        r["git_branch"] or "",
            "last":          (r["last_timestamp"] or "")[:16].replace("T", " "),
            "last_date":     (r["last_timestamp"] or "")[:10],
            "duration_min":  duration_min,
            "model":         r["model"] or "unknown",
            "turns":         r["turn_count"] or 0,
            "input":         r["total_input_tokens"] or 0,
            "output":        r["total_output_tokens"] or 0,
            "cache_read":    r["total_cache_read"] or 0,
            "cache_creation": r["total_cache_creation"] or 0,
        })

    # ── Per-project, per-model, per-day (from turns — accurate sub-agent routing) ─
    # The sessions table has one model per session (typically Sonnet). The turns
    # table has per-API-call attribution, so Haiku sub-agent calls show here.
    proj_model_rows = conn.execute("""
        SELECT
            COALESCE(s.project_name, 'unknown') as project,
            COALESCE(t.model, 'unknown')         as model,
            substr(t.timestamp, 1, 10)           as day,
            SUM(t.input_tokens)                  as input,
            SUM(t.output_tokens)                 as output,
            SUM(t.cache_read_tokens)             as cache_read,
            SUM(t.cache_creation_tokens)         as cache_creation,
            COUNT(*)                             as turns
        FROM turns t
        LEFT JOIN sessions s ON t.session_id = s.session_id
        WHERE t.timestamp IS NOT NULL
        GROUP BY s.project_name, t.model, day
        ORDER BY s.project_name, day, t.model
    """).fetchall()

    project_daily_by_model = [{
        "project":        r["project"],
        "model":          r["model"],
        "day":            r["day"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
    } for r in proj_model_rows]

    conn.close()

    return {
        "all_models":             all_models,
        "daily_by_model":         daily_by_model,
        "hourly_by_model":        hourly_by_model,
        "sessions_all":           sessions_all,
        "project_daily_by_model": project_daily_by_model,
        "generated_at":           datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pricing":                load_pricing(),
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Usage Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117;
    --card: #1a1d27;
    --border: #2a2d3a;
    --text: #e2e8f0;
    --muted: #8892a4;
    --accent: #d97757;
    --blue: #4f8ef7;
    --green: #4ade80;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }

  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--accent); }
  header .meta { color: var(--muted); font-size: 12px; }
  #rescan-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; margin-top: 4px; }
  #rescan-btn:hover { color: var(--text); border-color: var(--accent); }
  #rescan-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  #filter-bar { background: var(--card); border-bottom: 1px solid var(--border); padding: 10px 24px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .filter-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); white-space: nowrap; }
  .filter-sep { width: 1px; height: 22px; background: var(--border); flex-shrink: 0; }
  #model-checkboxes { display: flex; flex-wrap: wrap; gap: 6px; }
  .model-cb-label { display: flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 20px; border: 1px solid var(--border); cursor: pointer; font-size: 12px; color: var(--muted); transition: border-color 0.15s, color 0.15s, background 0.15s; user-select: none; }
  .model-cb-label:hover { border-color: var(--accent); color: var(--text); }
  .model-cb-label.checked { background: rgba(217,119,87,0.12); border-color: var(--accent); color: var(--text); }
  .model-cb-label input { display: none; }
  .filter-btn { padding: 3px 10px; border-radius: 4px; border: 1px solid var(--border); background: transparent; color: var(--muted); font-size: 11px; cursor: pointer; white-space: nowrap; }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  .filter-btn.active { background: rgba(217,119,87,0.15); border-color: var(--accent); color: var(--accent); }
  .range-group { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; flex-shrink: 0; }
  .range-btn { padding: 4px 13px; background: transparent; border: none; border-right: 1px solid var(--border); color: var(--muted); font-size: 12px; cursor: pointer; transition: background 0.15s, color 0.15s; }
  .range-btn:last-child { border-right: none; }
  .range-btn:hover { background: rgba(255,255,255,0.04); color: var(--text); }
  .range-btn.active { background: rgba(217,119,87,0.15); color: var(--accent); font-weight: 600; }
  input[type="date"] { background: var(--card); border: 1px solid var(--border); color: var(--text); padding: 3px 8px; border-radius: 4px; font-size: 12px; cursor: pointer; }
  input[type="date"]:focus { outline: none; border-color: var(--accent); }

  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .stat-card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .stat-card .value { font-size: 22px; font-weight: 700; }
  .stat-card .sub { color: var(--muted); font-size: 11px; margin-top: 4px; }

  /* Projects accordion */
  #projects-section { margin-bottom: 24px; }
  .section-heading { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
  .accordion { background: var(--card); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 8px; overflow: hidden; }
  .accordion-header { display: flex; align-items: center; gap: 12px; padding: 14px 20px; cursor: pointer; user-select: none; }
  .accordion-header:hover { background: rgba(255,255,255,0.02); }
  .accordion-arrow { color: var(--muted); font-size: 11px; flex-shrink: 0; width: 12px; }
  .accordion-name { font-weight: 600; flex: 1 1 auto; }
  .accordion-meta { color: var(--muted); font-size: 12px; flex-shrink: 0; }
  .accordion-body { display: none; border-top: 1px solid var(--border); }
  .accordion-body.open { display: block; }
  .accordion-content { display: flex; align-items: flex-start; }
  .sessions-table-wrap { flex: 1 1 auto; overflow-x: auto; padding: 16px; }
  .donut-wrap { flex: 0 0 260px; padding: 16px; border-left: 1px solid var(--border); display: flex; flex-direction: column; align-items: center; }
  .donut-title { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 10px; }

  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 7px 10px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); border-bottom: 1px solid var(--border); white-space: nowrap; }
  td { padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: 12px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .total-row td { border-top: 2px solid var(--border); border-bottom: none !important; font-weight: 600; }
  .model-tag { display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 10px; background: rgba(79,142,247,0.15); color: var(--blue); white-space: nowrap; max-width: 200px; overflow: hidden; text-overflow: ellipsis; }
  .cost { color: var(--green); font-family: monospace; }
  .cost-na { color: var(--muted); font-family: monospace; font-size: 11px; }
  .num { font-family: monospace; }
  .muted { color: var(--muted); }
  .mono { font-family: monospace; }

  /* Daily chart */
  #daily-section { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }
  #daily-section h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 16px; }
  .chart-wrap { position: relative; height: 300px; }

  footer { border-top: 1px solid var(--border); padding: 20px 24px; margin-top: 24px; }
  .footer-content { max-width: 1400px; margin: 0 auto; }
  .footer-content p { color: var(--muted); font-size: 12px; line-height: 1.7; margin-bottom: 4px; }
  .footer-content p:last-child { margin-bottom: 0; }
  .footer-content a { color: var(--blue); text-decoration: none; }
  .footer-content a:hover { text-decoration: underline; }
  code { font-family: monospace; font-size: 11px; background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px; }
</style>
</head>
<body>

<header>
  <h1>Claude Code Usage Dashboard</h1>
  <div class="meta" id="meta">Loading...</div>
  <button id="rescan-btn" onclick="triggerRescan()" title="Rebuild the database from scratch by re-scanning all JSONL files. Use if data looks stale or costs seem wrong.">&#x21bb; Rescan</button>
</header>

<div id="filter-bar">
  <div class="filter-label">Models</div>
  <div id="model-checkboxes"></div>
  <button class="filter-btn" onclick="selectAllModels()">All</button>
  <button class="filter-btn" onclick="clearAllModels()">None</button>
  <div class="filter-sep"></div>
  <div class="filter-label">Range</div>
  <div class="range-group">
    <button class="range-btn" data-range="week"       onclick="setRange('week')">This Week</button>
    <button class="range-btn" data-range="month"      onclick="setRange('month')">This Month</button>
    <button class="range-btn" data-range="prev-month" onclick="setRange('prev-month')">Prev Month</button>
    <button class="range-btn" data-range="7d"         onclick="setRange('7d')">7d</button>
    <button class="range-btn" data-range="30d"        onclick="setRange('30d')">30d</button>
    <button class="range-btn" data-range="90d"        onclick="setRange('90d')">90d</button>
    <button class="range-btn" data-range="all"        onclick="setRange('all')">All</button>
  </div>
  <div class="filter-sep"></div>
  <div class="filter-label">Custom</div>
  <input type="date" id="custom-start" onchange="onCustomDate()">
  <span style="color:var(--muted)">&#x2013;</span>
  <input type="date" id="custom-end" onchange="onCustomDate()">
  <div class="filter-sep"></div>
  <button id="group-parent-btn" class="filter-btn" onclick="toggleGroupByParent()" title="Merge sub-directories under their parent (e.g. finance/advisor + finance/executor → finance)">Group paths</button>
</div>

<div class="container">
  <div class="stats-row" id="stats-row"></div>
  <div id="projects-section"></div>
  <div id="daily-section">
    <h2 id="daily-chart-title">Daily Token Usage</h2>
    <div class="chart-wrap"><canvas id="chart-daily"></canvas></div>
  </div>
</div>

<footer>
  <div class="footer-content">
    <p>Cost estimates based on Anthropic API pricing (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) as of April 2026. Override rates with <code>python cli.py set-rate</code>. Only models containing <em>opus</em>, <em>sonnet</em>, or <em>haiku</em> are included in cost calculations. Actual costs for Pro/Max subscribers differ from API pricing.</p>
    <p>GitHub: <a href="https://github.com/phuryn/claude-usage" target="_blank">https://github.com/phuryn/claude-usage</a> &nbsp;&middot;&nbsp; License: MIT</p>
  </div>
</footer>

<script>
// ── Helpers ────────────────────────────────────────────────────────────────
function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

// ── State ──────────────────────────────────────────────────────────────────
let rawData        = null;
let PRICING        = {};
let selectedModels = new Set();
let selectedRange  = '30d';
let customStart    = null;   // 'YYYY-MM-DD' or null
let customEnd      = null;   // 'YYYY-MM-DD' or null
let projectExpandState = {}; // project name -> bool
let charts         = {};
let lastByProject  = [];     // [{project, sessions[], cost}]
let lastProjModelMap = {};   // project -> {model -> total tokens} (per-turn, not per-session)
let groupByParent  = false;  // merge parent/leaf entries under parent

// ── Pricing ────────────────────────────────────────────────────────────────
function isBillable(model) {
  if (!model) return false;
  const m = model.toLowerCase();
  return m.includes('opus') || m.includes('sonnet') || m.includes('haiku');
}

function getPricing(model) {
  if (!model || !PRICING) return null;
  if (PRICING[model]) return PRICING[model];
  for (const key of Object.keys(PRICING)) {
    if (model.startsWith(key)) return PRICING[key];
  }
  const m = model.toLowerCase();
  if (m.includes('opus'))   return PRICING['claude-opus-4-7'];
  if (m.includes('sonnet')) return PRICING['claude-sonnet-4-6'];
  if (m.includes('haiku'))  return PRICING['claude-haiku-4-5'];
  return null;
}

function calcCost(model, inp, out, cacheRead, cacheCreation) {
  if (!isBillable(model)) return 0;
  const p = getPricing(model);
  if (!p) return 0;
  return (
    inp           * p.input       / 1e6 +
    out           * p.output      / 1e6 +
    cacheRead     * p.cache_read  / 1e6 +
    cacheCreation * p.cache_write / 1e6
  );
}

// ── Formatting ─────────────────────────────────────────────────────────────
function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(2)+'B';
  if (n >= 1e6) return (n/1e6).toFixed(2)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
  return n.toLocaleString();
}
function fmtCost(c)    { return '$' + c.toFixed(4); }
function fmtCostBig(c) { return '$' + c.toFixed(2); }

// ── Chart colors ───────────────────────────────────────────────────────────
const TOKEN_COLORS = {
  input:          'rgba(79,142,247,0.8)',
  output:         'rgba(167,139,250,0.8)',
  cache_read:     'rgba(74,222,128,0.6)',
  cache_creation: 'rgba(251,191,36,0.6)',
};
const MODEL_COLORS = ['#d97757','#4f8ef7','#4ade80','#a78bfa','#fbbf24','#f472b6','#34d399','#60a5fa'];

// ── Time range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = {
  'week': 'This Week', 'month': 'This Month', 'prev-month': 'Previous Month',
  '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'all': 'All Time'
};
const VALID_RANGES = Object.keys(RANGE_LABELS);

function getRangeLabel() {
  if (customStart || customEnd) {
    return (customStart || '…') + ' – ' + (customEnd || '…');
  }
  return RANGE_LABELS[selectedRange] || selectedRange;
}

function rangeIncludesToday() {
  const today = new Date().toISOString().slice(0, 10);
  if (customStart || customEnd) {
    if (customStart && today < customStart) return false;
    if (customEnd   && today > customEnd)   return false;
    return true;
  }
  if (selectedRange === 'all') return true;
  const { start, end } = getRangeBounds(selectedRange);
  if (start && today < start) return false;
  if (end   && today > end)   return false;
  return true;
}

function getRangeBounds(range) {
  if (range === 'all') return { start: null, end: null };
  const today = new Date();
  const iso = d => d.toISOString().slice(0, 10);
  if (range === 'week') {
    const day = today.getDay();
    const diffToMon = day === 0 ? 6 : day - 1;
    const mon = new Date(today); mon.setDate(today.getDate() - diffToMon);
    const sun = new Date(mon);   sun.setDate(mon.getDate() + 6);
    return { start: iso(mon), end: iso(sun) };
  }
  if (range === 'month') {
    const start = new Date(today.getFullYear(), today.getMonth(), 1);
    const end   = new Date(today.getFullYear(), today.getMonth() + 1, 0);
    return { start: iso(start), end: iso(end) };
  }
  if (range === 'prev-month') {
    const start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
    const end   = new Date(today.getFullYear(), today.getMonth(), 0);
    return { start: iso(start), end: iso(end) };
  }
  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date(); d.setDate(d.getDate() - days);
  return { start: iso(d), end: null };
}

function readURLRange() {
  const p = new URLSearchParams(window.location.search).get('range');
  return VALID_RANGES.includes(p) ? p : '30d';
}

function setRange(range) {
  selectedRange = range;
  customStart = null;
  customEnd   = null;
  document.getElementById('custom-start').value = '';
  document.getElementById('custom-end').value   = '';
  document.querySelectorAll('.range-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.range === range)
  );
  updateURL();
  applyFilter();
  scheduleAutoRefresh();
}

function onCustomDate() {
  customStart = document.getElementById('custom-start').value || null;
  customEnd   = document.getElementById('custom-end').value   || null;
  if (customStart || customEnd) {
    document.querySelectorAll('.range-btn').forEach(btn => btn.classList.remove('active'));
  }
  applyFilter();
  scheduleAutoRefresh();
}

// ── Model filter ───────────────────────────────────────────────────────────
function modelPriority(m) {
  const ml = m.toLowerCase();
  if (ml.includes('opus'))   return 0;
  if (ml.includes('sonnet')) return 1;
  if (ml.includes('haiku'))  return 2;
  return 3;
}

function readURLModels(allModels) {
  const param = new URLSearchParams(window.location.search).get('models');
  if (!param) return new Set(allModels.filter(m => isBillable(m)));
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  return new Set(allModels.filter(m => fromURL.has(m)));
}

function isDefaultModelSelection(allModels) {
  const billable = allModels.filter(m => isBillable(m));
  if (selectedModels.size !== billable.length) return false;
  return billable.every(m => selectedModels.has(m));
}

function buildFilterUI(allModels) {
  const sorted = [...allModels].sort((a, b) => {
    const pa = modelPriority(a), pb = modelPriority(b);
    return pa !== pb ? pa - pb : a.localeCompare(b);
  });
  selectedModels = readURLModels(allModels);
  const container = document.getElementById('model-checkboxes');
  container.innerHTML = sorted.map(m => {
    const checked = selectedModels.has(m);
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${esc(m)}">
      <input type="checkbox" value="${esc(m)}" ${checked ? 'checked' : ''} onchange="onModelToggle(this)">
      ${esc(m)}
    </label>`;
  }).join('');
}

function onModelToggle(cb) {
  const label = cb.closest('label');
  if (cb.checked) { selectedModels.add(cb.value);    label.classList.add('checked'); }
  else            { selectedModels.delete(cb.value); label.classList.remove('checked'); }
  updateURL();
  applyFilter();
}

function selectAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = true; selectedModels.add(cb.value); cb.closest('label').classList.add('checked');
  });
  updateURL(); applyFilter();
}

function clearAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = false; selectedModels.delete(cb.value); cb.closest('label').classList.remove('checked');
  });
  updateURL(); applyFilter();
}

// ── Group-by-parent ────────────────────────────────────────────────────────
function normalizedProject(name) {
  if (!groupByParent) return name;
  const slash = name.indexOf('/');
  return slash >= 0 ? name.slice(0, slash) : name;
}

function toggleGroupByParent() {
  groupByParent = !groupByParent;
  projectExpandState = {};  // reset expand state — project keys change
  const btn = document.getElementById('group-parent-btn');
  if (btn) btn.classList.toggle('active', groupByParent);
  applyFilter();
}

// ── URL persistence ────────────────────────────────────────────────────────
function updateURL() {
  const allModels = Array.from(document.querySelectorAll('#model-checkboxes input')).map(cb => cb.value);
  const params = new URLSearchParams();
  if (selectedRange !== '30d') params.set('range', selectedRange);
  if (!isDefaultModelSelection(allModels)) params.set('models', Array.from(selectedModels).join(','));
  const search = params.toString() ? '?' + params.toString() : '';
  history.replaceState(null, '', window.location.pathname + search);
}

// ── Filter & aggregate ─────────────────────────────────────────────────────
function applyFilter() {
  if (!rawData) return;

  let start, end;
  if (customStart || customEnd) {
    start = customStart;
    end   = customEnd;
  } else {
    const bounds = getRangeBounds(selectedRange);
    start = bounds.start;
    end   = bounds.end;
  }

  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );

  const filteredSessions = rawData.sessions_all.filter(s =>
    selectedModels.has(s.model) && (!start || s.last_date >= start) && (!end || s.last_date <= end)
  );

  // Group sessions by (normalized) project, sorted by cost desc
  const projMap = {};
  for (const s of filteredSessions) {
    const key = normalizedProject(s.project);
    if (!projMap[key]) projMap[key] = { project: key, sessions: [], cost: 0 };
    projMap[key].sessions.push(s);
    projMap[key].cost += calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
  }
  lastByProject = Object.values(projMap).sort((a, b) => b.cost - a.cost);

  // Build per-project, per-model token map from per-turn data (respects date + model filter).
  // This captures sub-agent Haiku calls that the session-level model field misses.
  lastProjModelMap = {};
  for (const r of (rawData.project_daily_by_model || [])) {
    if (!selectedModels.has(r.model)) continue;
    if (start && r.day < start) continue;
    if (end   && r.day > end)   continue;
    const key = normalizedProject(r.project);
    if (!lastProjModelMap[key]) lastProjModelMap[key] = {};
    lastProjModelMap[key][r.model] =
      (lastProjModelMap[key][r.model] || 0) + r.input + r.output;
  }

  // Totals from filtered sessions
  const totals = {
    sessions:       filteredSessions.length,
    turns:          filteredSessions.reduce((acc, s) => acc + s.turns, 0),
    input:          filteredSessions.reduce((acc, s) => acc + s.input, 0),
    output:         filteredSessions.reduce((acc, s) => acc + s.output, 0),
    cache_read:     filteredSessions.reduce((acc, s) => acc + s.cache_read, 0),
    cache_creation: filteredSessions.reduce((acc, s) => acc + s.cache_creation, 0),
    cost:           filteredSessions.reduce((acc, s) => acc + calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation), 0),
  };

  document.getElementById('daily-chart-title').textContent =
    'Daily Token Usage — ' + getRangeLabel();

  renderStats(totals);
  renderProjects(lastByProject);
  renderDailyChart(filteredDaily, start, end);
}

// ── Renderers ──────────────────────────────────────────────────────────────
function renderStats(t) {
  const sub = getRangeLabel().toLowerCase();
  const stats = [
    { label: 'Sessions',       value: t.sessions.toLocaleString(), sub },
    { label: 'Turns',          value: fmt(t.turns),                sub },
    { label: 'Input Tokens',   value: fmt(t.input),                sub },
    { label: 'Output Tokens',  value: fmt(t.output),               sub },
    { label: 'Cache Read',     value: fmt(t.cache_read),           sub: 'from prompt cache' },
    { label: 'Cache Creation', value: fmt(t.cache_creation),       sub: 'writes to prompt cache' },
    { label: 'Est. Cost',      value: fmtCostBig(t.cost),          sub: 'API pricing', color: '#4ade80' },
  ];
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="label">${s.label}</div>
      <div class="value" style="${s.color ? 'color:' + s.color : ''}">${esc(s.value)}</div>
      ${s.sub ? `<div class="sub">${esc(s.sub)}</div>` : ''}
    </div>
  `).join('');
}

function renderProjectSessionsTable(sessions, groupKey) {
  const sorted = [...sessions].sort((a, b) => b.last_date.localeCompare(a.last_date));
  // Show 'Dir' column only when sessions span multiple sub-directories under the same parent
  const subDirs = new Set(sorted.map(s => s.project));
  const showDirCol = groupByParent && subDirs.size > 1;

  const totals = sorted.reduce((acc, s) => {
    acc.turns          += s.turns;
    acc.input          += s.input;
    acc.output         += s.output;
    acc.cache_read     += s.cache_read;
    acc.cache_creation += s.cache_creation;
    acc.cost           += calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    return acc;
  }, { turns: 0, input: 0, output: 0, cache_read: 0, cache_creation: 0, cost: 0 });

  const rows = sorted.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    const costCell = isBillable(s.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const leaf = s.project.includes('/') ? s.project.split('/').pop() : s.project;
    const dirCell = showDirCol ? `<td class="muted">${esc(leaf)}</td>` : '';
    return `<tr>
      <td class="muted mono">${esc(s.session_id)}&hellip;</td>
      ${dirCell}
      <td class="muted">${esc(s.last_date)}</td>
      <td class="muted">${s.duration_min}m</td>
      <td><span class="model-tag" title="${esc(s.model)}">${esc(s.model)}</span></td>
      <td class="num">${s.turns}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      <td class="num">${fmt(s.cache_read)}</td>
      <td class="num">${fmt(s.cache_creation)}</td>
      ${costCell}
    </tr>`;
  }).join('');

  const totalColspan = showDirCol ? 5 : 4;
  const totalRow = `<tr class="total-row">
    <td colspan="${totalColspan}" class="muted">Total</td>
    <td class="num">${totals.turns}</td>
    <td class="num">${fmt(totals.input)}</td>
    <td class="num">${fmt(totals.output)}</td>
    <td class="num">${fmt(totals.cache_read)}</td>
    <td class="num">${fmt(totals.cache_creation)}</td>
    <td class="cost">${fmtCost(totals.cost)}</td>
  </tr>`;

  const dirHeader = showDirCol ? '<th>Dir</th>' : '';
  return `<table>
    <thead><tr>
      <th>Session</th>${dirHeader}<th>Date</th><th>Dur</th><th>Model</th>
      <th>Turns</th><th>Input</th><th>Output</th>
      <th>Cache Read</th><th>Cache Creation</th><th>Est. Cost</th>
    </tr></thead>
    <tbody>${rows}${totalRow}</tbody>
  </table>`;
}

function renderProjectDonut(id, modelTokens) {
  const models = Object.keys(modelTokens).sort((a, b) => modelTokens[b] - modelTokens[a]);
  if (!models.length) return;

  const canvas = document.getElementById('donut-' + id);
  if (!canvas) return;
  const key = 'donut_' + id;
  if (charts[key]) charts[key].destroy();

  charts[key] = new Chart(canvas.getContext('2d'), {
    type: 'doughnut',
    data: {
      labels: models,
      datasets: [{
        data: models.map(m => modelTokens[m]),
        backgroundColor: models.map((_, i) => MODEL_COLORS[i % MODEL_COLORS.length]),
        borderWidth: 2,
        borderColor: '#1a1d27',
      }]
    },
    options: {
      responsive: false,
      plugins: {
        legend: { position: 'bottom', labels: { color: '#8892a4', boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${fmt(ctx.raw)} tokens` } },
      }
    }
  });
}

function toggleProject(idx) {
  const proj = lastByProject[idx];
  if (!proj) return;
  const project = proj.project;

  const isNowOpen = !projectExpandState[project];
  projectExpandState[project] = isNowOpen;

  const body = document.getElementById('accordion-body-' + idx);
  if (!body) return;
  const arrow = body.previousElementSibling.querySelector('.accordion-arrow');

  if (isNowOpen) {
    body.classList.add('open');
    if (arrow) arrow.textContent = '▼';
    renderProjectDonut(String(idx), lastProjModelMap[proj.project] || {});
  } else {
    body.classList.remove('open');
    if (arrow) arrow.textContent = '▶';
    const key = 'donut_' + idx;
    if (charts[key]) { charts[key].destroy(); delete charts[key]; }
  }
}

function renderProjects(byProject) {
  // Destroy all existing donut charts before rebuilding
  for (const key of Object.keys(charts)) {
    if (key.startsWith('donut_')) { charts[key].destroy(); delete charts[key]; }
  }

  const section = document.getElementById('projects-section');

  if (!byProject.length) {
    section.innerHTML = '<div style="color:var(--muted);padding:20px;background:var(--card);border:1px solid var(--border);border-radius:8px">No projects in this range.</div>';
    return;
  }

  // Auto-expand top project on first render
  if (Object.keys(projectExpandState).length === 0) {
    projectExpandState[byProject[0].project] = true;
  }

  const parts = ['<div class="section-heading">Projects</div>'];

  for (let idx = 0; idx < byProject.length; idx++) {
    const p = byProject[idx];
    const isOpen = !!projectExpandState[p.project];
    const pTurns = p.sessions.reduce((sum, s) => sum + s.turns, 0);
    const dates = p.sessions.map(s => s.last_date).filter(Boolean).sort();
    const dateRange = dates.length === 0 ? '' :
      dates[0] === dates[dates.length - 1] ? dates[0] :
      dates[0] + '–' + dates[dates.length - 1];
    const sessionWord = p.sessions.length === 1 ? 'session' : 'sessions';

    parts.push(`
      <div class="accordion">
        <div class="accordion-header" onclick="toggleProject(${idx})">
          <span class="accordion-arrow">${isOpen ? '▼' : '▶'}</span>
          <span class="accordion-name">${esc(p.project)}</span>
          <span class="accordion-meta">${p.sessions.length} ${sessionWord} &middot; ${pTurns.toLocaleString()} turns &middot; ${esc(dateRange)} &middot; <span class="cost">${fmtCost(p.cost)} est</span></span>
        </div>
        <div class="accordion-body ${isOpen ? 'open' : ''}" id="accordion-body-${idx}">
          <div class="accordion-content">
            <div class="sessions-table-wrap">${renderProjectSessionsTable(p.sessions, p.project)}</div>
            <div class="donut-wrap">
              <div class="donut-title">By Model</div>
              <canvas id="donut-${idx}" width="228" height="228"></canvas>
            </div>
          </div>
        </div>
      </div>`);
  }

  section.innerHTML = parts.join('');

  // Render donuts for all open projects
  for (let idx = 0; idx < byProject.length; idx++) {
    const p = byProject[idx];
    if (projectExpandState[p.project]) {
      renderProjectDonut(String(idx), lastProjModelMap[p.project] || {});
    }
  }
}

function renderDailyChart(filteredDaily, rangeStart, rangeEnd) {
  // Aggregate by day (includes per-model cost)
  const dailyMap = {};
  for (const r of filteredDaily) {
    if (!dailyMap[r.day]) dailyMap[r.day] = { day: r.day, input: 0, output: 0, cache_read: 0, cache_creation: 0, cost: 0 };
    const d = dailyMap[r.day];
    d.input          += r.input;
    d.output         += r.output;
    d.cache_read     += r.cache_read;
    d.cache_creation += r.cache_creation;
    d.cost           += calcCost(r.model, r.input, r.output, r.cache_read, r.cache_creation);
  }

  const ctx = document.getElementById('chart-daily').getContext('2d');
  if (charts.daily) { charts.daily.destroy(); charts.daily = null; }

  const today = new Date().toISOString().slice(0, 10);
  const dataKeys = Object.keys(dailyMap).sort();

  // Determine the date span to fill.
  // Named ranges supply a start; "all" supplies null → span data to today.
  let effectiveStart = rangeStart;
  let effectiveEnd   = rangeEnd || today;
  if (!effectiveStart) {
    if (!dataKeys.length) return;
    effectiveStart = dataKeys[0];   // oldest day with data
  }

  // Build a dense array covering every calendar day in the span.
  const daily = [];
  const cur     = new Date(effectiveStart + 'T12:00:00Z');
  const endDate = new Date(effectiveEnd   + 'T12:00:00Z');
  while (cur <= endDate) {
    const dayStr = cur.toISOString().slice(0, 10);
    daily.push(dailyMap[dayStr] || { day: dayStr, input: 0, output: 0, cache_read: 0, cache_creation: 0, cost: 0 });
    cur.setDate(cur.getDate() + 1);
  }
  if (!daily.length) return;

  charts.daily = new Chart(ctx, {
    data: {
      labels: daily.map(d => d.day),
      datasets: [
        { type: 'bar',  label: 'Input',          data: daily.map(d => d.input),          backgroundColor: TOKEN_COLORS.input,          stack: 'tokens', yAxisID: 'y' },
        { type: 'bar',  label: 'Output',         data: daily.map(d => d.output),         backgroundColor: TOKEN_COLORS.output,         stack: 'tokens', yAxisID: 'y' },
        { type: 'bar',  label: 'Cache Read',     data: daily.map(d => d.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     stack: 'tokens', yAxisID: 'y' },
        { type: 'bar',  label: 'Cache Creation', data: daily.map(d => d.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, stack: 'tokens', yAxisID: 'y' },
        { type: 'line', label: 'Est. Cost ($)',  data: daily.map(d => d.cost),
          borderColor: '#4ade80', backgroundColor: 'transparent', borderWidth: 2,
          pointRadius: 3, tension: 0, yAxisID: 'y1', showLine: true, pointStyle: 'circle' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: '#8892a4', boxWidth: 12 } },
        tooltip: {
          callbacks: {
            label: (item) => {
              if (item.datasetIndex === 4) return ' Cost: $' + item.parsed.y.toFixed(4);
              return ' ' + item.dataset.label + ': ' + fmt(item.parsed.y);
            }
          }
        }
      },
      scales: {
        x:  { ticks: { color: '#8892a4', maxTicksLimit: 15 }, grid: { color: '#2a2d3a' } },
        y:  { position: 'left',  stacked: true, beginAtZero: true,
              ticks: { color: '#4f8ef7', callback: v => fmt(v) }, grid: { color: '#2a2d3a' },
              title: { display: true, text: 'Tokens', color: '#4f8ef7' } },
        y1: { position: 'right', beginAtZero: true,
              ticks: { color: '#4ade80', callback: v => '$' + v.toFixed(2) }, grid: { drawOnChartArea: false },
              title: { display: true, text: 'Est. Cost ($)', color: '#4ade80' } },
      }
    }
  });
}

// ── Rescan ────────────────────────────────────────────────────────────────
async function triggerRescan() {
  const btn = document.getElementById('rescan-btn');
  btn.disabled = true;
  btn.textContent = '↻ Scanning...';
  try {
    const resp = await fetch('/api/rescan', { method: 'POST' });
    const d = await resp.json();
    btn.textContent = '↻ Rescan (' + d.new + ' new, ' + d.updated + ' updated)';
    await loadData();
  } catch(e) {
    btn.textContent = '↻ Rescan (error)';
    console.error(e);
  }
  setTimeout(() => { btn.textContent = '↻ Rescan'; btn.disabled = false; }, 3000);
}

// ── Data loading ───────────────────────────────────────────────────────────
async function loadData() {
  try {
    const resp = await fetch('/api/data');
    const d = await resp.json();
    if (d.error) {
      document.body.innerHTML = '<div style="padding:40px;color:#f87171">' + esc(d.error) + '</div>';
      return;
    }

    const isFirstLoad = rawData === null;
    rawData  = d;
    PRICING  = d.pricing || {};

    if (isFirstLoad) {
      selectedRange = readURLRange();
      document.querySelectorAll('.range-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.range === selectedRange)
      );
      buildFilterUI(d.all_models);
    }

    const refreshNote = rangeIncludesToday() ? ' · Auto-refresh in 30s' : '';
    document.getElementById('meta').textContent = 'Updated: ' + d.generated_at + refreshNote;

    applyFilter();
  } catch(e) {
    console.error(e);
  }
}

let autoRefreshTimer = null;
function scheduleAutoRefresh() {
  if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
  if (rangeIncludesToday()) {
    autoRefreshTimer = setInterval(loadData, 30000);
  }
}

loadData();
scheduleAutoRefresh();
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

        elif self.path == "/api/data":
            data = get_dashboard_data()
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/rescan":
            # Full rebuild: delete DB and rescan from scratch.
            # Pass DB_PATH / DEFAULT_PROJECTS_DIRS explicitly so tests that
            # patch the module globals are honored (scan's defaults are
            # frozen at def time and would otherwise target the real paths).
            import scanner
            db_path = DB_PATH
            if db_path.exists():
                db_path.unlink()
            result = scanner.scan(
                db_path=db_path,
                projects_dirs=scanner.DEFAULT_PROJECTS_DIRS,
                verbose=False,
            )
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def serve(host=None, port=None):
    host = host or os.environ.get("HOST", "localhost")
    port = port or int(os.environ.get("PORT", "8080"))
    server = HTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
