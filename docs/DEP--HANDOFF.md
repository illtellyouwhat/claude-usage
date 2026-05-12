# Claude Usage Dashboard — Implementation Handoff

## Context for the incoming session

This repo is a local Claude Code usage tracker at `/home/phil/github/cloned-tools/claude-usage/`.
The user has agreed on a complete dashboard redesign. This document is the complete spec and execution plan.
**Start immediately — no questions needed, all decisions are recorded here.**

---

## What exists today

### Files
- `scanner.py` — scans `~/.claude/projects/**/*.jsonl`, writes to `~/.claude/usage.db` (SQLite). **Do not modify.**
- `cli.py` — CLI entry point. Has `scan`, `today`, `week`, `stats`, `dashboard` commands. **Needs one addition: `set-rate` command.**
- `dashboard.py` — Python HTTP server + large inline HTML template. **Complete rewrite of the HTML template.** Python server code (DashboardHandler class, `serve()`, `get_dashboard_data()`) stays mostly the same.
- `docs/overview.md` — **Create this file** (see spec below).
- `tests/` — Do not modify tests unless they break after changes.

### Bug already fixed in this session
Line 745 of `dashboard.py` referenced `cutoff` (undefined JS variable) inside `applyFilter()`.
Fixed to use `start`/`end` which are the defined range bounds. This is already committed.

### Database state
- Path: `~/.claude/usage.db`
- Tables: `sessions`, `turns`, `processed_files`
- All 8 JSONL files are now scanned (184 turns, 7 sessions)
- Models in DB: `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`, `claude-sonnet-4-20250514`

### Key data shapes (from `get_dashboard_data()`)
```
all_models: list of exact model name strings (e.g. "claude-sonnet-4-6")
daily_by_model: [{day, model, input, output, cache_read, cache_creation, turns}]
hourly_by_model: [{day, hour, model, output, turns}]
sessions_all: [{session_id (8 chars), project, branch, last, last_date, duration_min, model, turns, input, output, cache_read, cache_creation}]
```

---

## Decisions made by the user (do not re-ask)

1. **Replace** the current dashboard entirely — new layout, not a tab.
2. **No Pro plan tracking** — user will compare manually.
3. **Pricing** stays hardcoded but with a rate-override file mechanism and CLI command.
4. **Model grouping** on donut charts: exact model version strings (granular, not collapsed to family).
5. **Daily bar chart**: one global chart at the bottom, not per-project.
6. **Custom date range**: add date picker inputs alongside the existing range buttons.
7. **Project view**: collapsible accordions, sorted by estimated total cost descending.

---

## Implementation plan — execute in this order

### Step 1: Rate override system

**New file: `~/.claude/claude_usage_rates.json`** — written by CLI, read at dashboard serve time.
Format matches the existing `PRICING` dict in `cli.py`:
```json
{
  "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75}
}
```
Only overridden models need entries — others fall back to defaults.

**In `dashboard.py`** — add a `load_pricing()` function:
```python
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
```

In `get_dashboard_data()`, call `load_pricing()` and include it in the returned dict:
```python
return {
    ...existing fields...,
    "pricing": load_pricing(),
}
```

Then in the JS `HTML_TEMPLATE`, instead of the hardcoded `const PRICING = {...}`, inject:
```javascript
const PRICING = __PRICING_JSON__;
```
And in `DashboardHandler.do_GET` for `/api/data`, the JSON response already includes `pricing`.
The JS reads it from `rawData.pricing` and stores as `let PRICING = {}` that gets set on first data load.

**In `cli.py`** — add `cmd_set_rate` and `cmd_rates`:

```python
RATES_OVERRIDE_PATH = Path.home() / ".claude" / "claude_usage_rates.json"

def cmd_rates():
    """Print current effective pricing (defaults merged with any overrides)."""
    overrides = {}
    if RATES_OVERRIDE_PATH.exists():
        try:
            overrides = json.loads(RATES_OVERRIDE_PATH.read_text())
        except Exception:
            pass
    import json as _json
    print("\nCurrent effective rates ($ per 1M tokens):")
    hr()
    effective = dict(PRICING)
    effective.update(overrides)
    for model, rates in sorted(effective.items()):
        tag = " [overridden]" if model in overrides else ""
        print(f"  {model:<28}  in={rates['input']:.2f}  out={rates['output']:.2f}  "
              f"cache_read={rates['cache_read']:.2f}  cache_write={rates['cache_write']:.2f}{tag}")
    hr()
    print()

def cmd_set_rate(model, input_rate, output_rate, cache_read_rate, cache_write_rate):
    """Write a rate override for one model to the override file."""
    import json as _json
    overrides = {}
    if RATES_OVERRIDE_PATH.exists():
        try:
            overrides = _json.loads(RATES_OVERRIDE_PATH.read_text())
        except Exception:
            pass
    overrides[model] = {
        "input": float(input_rate),
        "output": float(output_rate),
        "cache_read": float(cache_read_rate),
        "cache_write": float(cache_write_rate),
    }
    RATES_OVERRIDE_PATH.write_text(_json.dumps(overrides, indent=2))
    print(f"Rate override saved for {model}:")
    print(f"  input={input_rate}  output={output_rate}  cache_read={cache_read_rate}  cache_write={cache_write_rate}")
```

CLI parsing for `set-rate`:
```
python cli.py set-rate --model claude-sonnet-4-6 --input 3.00 --output 15.00 --cache-read 0.30 --cache-write 3.75
python cli.py rates
```

Add both to the `COMMANDS` dict and `USAGE` string.

---

### Step 2: Rewrite `dashboard.py` HTML template

The Python server (`DashboardHandler`, `serve()`, `get_dashboard_data()`) changes only slightly:
- Add `load_pricing()` function (see Step 1)
- Include `"pricing": load_pricing()` in `get_dashboard_data()` return value

The `HTML_TEMPLATE` is a complete rewrite. Structure:

#### HTML structure
```html
<header>  <!-- title, rescan btn, "Updated: ..." -->
<div id="filter-bar">  <!-- model checkboxes, range buttons, custom date pickers -->
<div class="container">
  <div id="stats-row">  <!-- 7 stat cards -->
  <div id="projects-section">  <!-- accordion per project -->
  <div id="daily-section">  <!-- global daily bar chart -->
</div>
<footer>  <!-- pricing note, github link -->
```

#### Filter bar additions (beyond current)
Add two date inputs for custom range:
```html
<div class="filter-sep"></div>
<div class="filter-label">Custom</div>
<input type="date" id="custom-start" onchange="onCustomDate()">
<span style="color:var(--muted)">–</span>
<input type="date" id="custom-end" onchange="onCustomDate()">
```
When a custom date is entered, deactivate the range buttons and use the custom range.
When a range button is clicked, clear the custom inputs.

#### Stat cards (same 7 as now)
Sessions | Turns | Input | Output | Cache Read | Cache Creation | Est. Cost

#### Projects section
```
Projects (sorted by estimated cost, highest first)
[For each project:]
  ┌─ project-header (clickable to expand/collapse) ─────────────────────────────────┐
  │  ▼ github/cloned-tools          3 sessions · 184 turns · Apr 28–29 · $4.23 est │
  └─────────────────────────────────────────────────────────────────────────────────┘
  [expanded body - display:flex row]
  ┌─ sessions table (flex: 1 1 auto) ──────┐  ┌─ model donut (flex: 0 0 260px) ─┐
  │ Session  Date  Dur  Model  Turns  In   │  │  [Chart.js doughnut]            │
  │   Out   Cache  Cost                   │  │  [legend below]                 │
  │ ...rows...                            │  └─────────────────────────────────┘
  │ TOTAL    —     —    —      N   XK  XK │
  └────────────────────────────────────────┘
```

**Sessions table columns:** Session (8-char ID), Date, Duration, Model (tag), Turns, Input, Output, Cache Read, Cache Creation, Est. Cost

**Donut chart:** Breakdown of `input + output` tokens by exact model string. Use `MODEL_COLORS` array. One donut per project, rendered into `<canvas id="donut-{project_slug}">`.

**Project header click** toggles a CSS class `open` on the accordion body. Default state: all collapsed. On first load, auto-expand the project with the highest cost.

**Project sorting:** Computed from `sessions_all` filtered by current range + model selection. Sort by sum of `calcCost(...)` across all sessions for that project, descending.

#### Global daily timeline (below projects)
Title: "Daily Token Usage — {range label}"

Stacked bar chart using Chart.js:
- X axis: dates in range
- Y axis (left): token count — stacked bars: input (blue), output (purple), cache_read (green), cache_creation (yellow)
- Y axis (right): cost ($) — line overlay showing estimated cost per day
- Cost annotation: render the $ cost as a small label above each bar group (use Chart.js `datalabels` plugin OR just put it as a dataset on the right Y axis as a line with point labels visible)

If `datalabels` plugin is not available (don't add CDN dependencies beyond what's already there: Chart.js 4.4.0), use the right Y axis line approach with `pointStyle: false` and `tension: 0` and `showLine: true`, displaying cost on the right axis only. This avoids needing an extra plugin.

#### JS state and data flow

```javascript
let rawData = null;         // from /api/data
let PRICING = {};           // set from rawData.pricing on first load
let selectedModels = new Set();
let selectedRange = '30d';
let customStart = null;     // string 'YYYY-MM-DD' or null
let customEnd = null;       // string 'YYYY-MM-DD' or null
let projectExpandState = {}; // project -> bool (open/closed)
let charts = {};            // keyed by chart id string
```

`applyFilter()` flow:
1. Compute `{ start, end }` from either custom dates or `getRangeBounds(selectedRange)`
2. Filter `rawData.daily_by_model` → `filteredDaily`
3. Filter `rawData.sessions_all` → `filteredSessions`
4. Group `filteredSessions` by `s.project` → `byProject` (array sorted by total cost desc)
5. Compute totals from `byProject`
6. Call all renderers:
   - `renderStats(totals)`
   - `renderProjects(byProject, filteredDaily)`  ← new
   - `renderDailyChart(filteredDaily)`  ← replaces old daily chart

`renderProjects(byProject, filteredDaily)` — builds the accordion HTML, then for each open project calls `renderProjectDonut(project, sessions)`.

**Important:** When the filter changes, existing donut charts must be destroyed before new ones are created. Track them in `charts['donut_' + slug]`.

#### Pricing in JS

On first data load: `PRICING = rawData.pricing;`

`getPricing(model)` and `calcCost(model, inp, out, cacheRead, cacheCreation)` functions are identical to current, just using the `PRICING` object populated from the API.

`isBillable(model)` — same logic as now (contains 'opus', 'sonnet', or 'haiku').

#### Color scheme
Keep existing dark theme vars:
```css
--bg: #0f1117; --card: #1a1d27; --border: #2a2d3a;
--text: #e2e8f0; --muted: #8892a4; --accent: #d97757;
--blue: #4f8ef7; --green: #4ade80;
```

MODEL_COLORS array (same 8 colors as now):
`['#d97757','#4f8ef7','#4ade80','#a78bfa','#fbbf24','#f472b6','#34d399','#60a5fa']`

---

### Step 3: Create `docs/overview.md`

See separate spec in this document below.

---

### Step 4: Update `README.md` commands section (if it has one)

Add `set-rate` and `rates` commands to whatever command reference exists.

---

## `docs/overview.md` spec

Create `/home/phil/github/cloned-tools/claude-usage/docs/overview.md`

Sections:

### 1. What this tool does
Brief: scans Claude Code's local JSONL transcript files → SQLite DB → local web dashboard + CLI.
Mention: JSONL files live at `~/.claude/projects/**/*.jsonl`, DB at `~/.claude/usage.db`.

### 2. Token types and what they mean

**Input tokens** — The raw text of your prompt sent to the model each turn. Billed at the standard input rate.

**Output tokens** — Tokens the model generates in its response. Most expensive category per token.

**Cache read tokens** — When Claude Code sends a long context (system prompt, file contents, prior conversation), Anthropic's prompt cache may already have it stored. Cache reads are billed at ~10% of the standard input rate. High cache read volume is good — it means you're saving money vs. re-sending the same context.

**Cache creation tokens** — The first time a context is cached, you pay a one-time write premium (~25% above input rate). On subsequent turns, those same tokens are read from cache at 10% of the input rate. Net effect: caching saves money on long sessions.

**How they add up to cost:**
```
cost = (input_tokens × input_rate)
     + (output_tokens × output_rate)
     + (cache_read_tokens × cache_read_rate)
     + (cache_creation_tokens × cache_write_rate)
     (all rates are per 1,000,000 tokens)
```

### 3. Pricing

Default rates are hardcoded (Anthropic API pricing, April 2026). You can override any model's rates via CLI without editing code:

```bash
python cli.py rates                          # view current effective rates
python cli.py set-rate \
  --model claude-sonnet-4-6 \
  --input 3.00 \
  --output 15.00 \
  --cache-read 0.30 \
  --cache-write 3.75
```

Overrides are stored in `~/.claude/claude_usage_rates.json`. Defaults apply to any model not overridden.

**Note on Pro vs API pricing:** The dashboard shows API pricing. Claude Pro/Max subscribers pay a flat monthly rate, not per-token. Use the estimated cost column to understand what your usage *would* cost on API billing — helpful for deciding whether the flat subscription rate is worthwhile for your usage volume.

### 4. CLI commands reference

```
python cli.py scan [--projects-dir PATH]
    Scan JSONL files and update the database.
    Run this if you want to refresh data without opening the dashboard.

python cli.py today
    Print today's token usage and estimated cost by model.

python cli.py week
    Print the last 7 days: per-day breakdown and per-model totals.

python cli.py stats
    Print all-time statistics: sessions, tokens, cost by model and project.

python cli.py dashboard [--projects-dir PATH] [--host HOST] [--port PORT]
    Scan then start the web dashboard (default: http://localhost:8080).
    Opens a browser automatically.

python cli.py rates
    Show current effective pricing rates (defaults + any overrides).

python cli.py set-rate --model MODEL --input N --output N --cache-read N --cache-write N
    Override pricing for a specific model ($ per 1M tokens).
    Example: python cli.py set-rate --model claude-sonnet-4-6 --input 3.00 --output 15.00 --cache-read 0.30 --cache-write 3.75
```

### 5. Dashboard layout

Describe the filter bar, stat cards, project accordion, and global daily chart. Reference the key interactions (custom date range, model filter, expand/collapse).

### 6. Model routing and cost

Explain that Claude Code routes to different models depending on the task: Haiku for quick/cheap operations, Sonnet for most tasks, Opus for the heaviest reasoning. The per-project donut chart shows which model handled which share of tokens — misrouting to Opus when Sonnet would suffice can significantly increase cost.

---

## Non-goals / out of scope

- No changes to `scanner.py`
- No Pro plan usage limits / progress bars
- No dynamic pricing API lookup
- No test changes unless tests break

---

## How to restart and execute

Start a new Claude Code session with:
```
claude --dangerously-skip-permissions
```

Then instruct it:
> "Read HANDOFF.md and implement everything in it. Start with Step 1, then Step 2, then Step 3. Run `python cli.py dashboard` to verify the dashboard works before finishing."

The session will have write permission to all files without prompting.

---

## Files to touch

| File | Change |
|------|--------|
| `dashboard.py` | Add `load_pricing()`, add `"pricing"` to API response, rewrite `HTML_TEMPLATE` |
| `cli.py` | Add `cmd_set_rate`, `cmd_rates`, update `COMMANDS` dict and `USAGE` string |
| `docs/overview.md` | Create new |
| `README.md` | Add new commands if a command reference section exists |

---

*Generated: 2026-04-29 — do not delete until implementation is complete and verified.*
