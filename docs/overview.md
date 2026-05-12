# Claude Usage Dashboard — Overview

A local tool that tracks your Claude Code token usage, estimates API-equivalent costs, and visualizes activity by project and session.

---

## How it works

Claude Code writes a JSONL transcript file for every session into `~/.claude/projects/`. Each line is a JSON record for one turn (your message or the assistant's response), including the model used and token counts from the Anthropic API.

This tool scans those files, stores the data in a local SQLite database (`~/.claude/usage.db`), and serves a web dashboard and CLI summaries on top of it. Nothing leaves your machine.

---

## Token types

Every API response includes a usage breakdown with four token counts. Understanding what each one means helps interpret the cost estimates.

### Input tokens
The raw text of your prompt — your message, the system prompt, any file contents or tool results included in context. Billed at the standard input rate each turn.

### Output tokens
Tokens the model generates in its response. Output rates are significantly higher than input rates (roughly 5× on Sonnet). Keeping responses focused reduces cost more than reducing context does.

### Cache read tokens
Anthropic's prompt cache stores the "stable" part of your context (system prompt, earlier conversation, large file contents) so it doesn't have to be re-processed every turn. When a cache hit occurs, those tokens are billed at roughly **10% of the input rate** instead of 100%.

A high cache read count is a good sign — it means you're getting expensive context processing done cheaply. Long sessions where the same codebase context is reused across many turns will show a large cache read volume.

### Cache creation tokens
The first time a block of context is written into the prompt cache, you pay a one-time **write premium of ~25% above the input rate**. On every subsequent turn where that same context is reused, it becomes a cache read at 10% of input.

Net effect: the write cost is recovered after roughly 2–3 reads. Long sessions with large, stable context are net positive from caching.

### How they add up to cost

```
turn cost = (input_tokens       × input_rate / 1,000,000)
          + (output_tokens      × output_rate / 1,000,000)
          + (cache_read_tokens  × cache_read_rate / 1,000,000)
          + (cache_creation_tokens × cache_write_rate / 1,000,000)
```

All rates are in USD per million tokens.

---

## Pricing

Default rates reflect Anthropic API pricing as of April 2026. They are hardcoded but overridable per-model via CLI without editing any source files.

### Default rates ($ per 1M tokens)

| Model | Input | Output | Cache Read | Cache Write |
|-------|------:|-------:|-----------:|------------:|
| claude-opus-4-x | $5.00 | $25.00 | $0.50 | $6.25 |
| claude-sonnet-4-x | $3.00 | $15.00 | $0.30 | $3.75 |
| claude-haiku-4-x | $1.00 | $5.00 | $0.10 | $1.25 |

### Updating rates when Anthropic changes pricing

```bash
# View current effective rates (defaults + any overrides)
python cli.py rates

# Override a specific model's rates
python cli.py set-rate \
  --model claude-sonnet-4-6 \
  --input 3.00 \
  --output 15.00 \
  --cache-read 0.30 \
  --cache-write 3.75
```

Overrides are saved to `~/.claude/claude_usage_rates.json`. Any model not in that file falls back to the built-in defaults. The dashboard picks up changes on next page load (or rescan).

### Pro vs API pricing

The cost estimates shown reflect **API pricing**, not Pro/Max subscription pricing. Pro subscribers pay a flat monthly rate with no per-token billing. Use the estimated cost column to understand what your usage volume *would* cost on a pay-per-token basis — this is the primary signal for deciding whether your subscription is a good deal relative to your actual usage.

---

## Model routing and cost

Claude Code automatically routes requests to different model sizes depending on task complexity. Understanding the routing helps diagnose unexpected cost:

- **Haiku** — fast, cheap, used for quick tool calls, short lookups, simple edits
- **Sonnet** — the workhorse, used for most coding tasks
- **Opus** — heavyweight reasoning, used for complex multi-step problems

The per-project donut chart in the dashboard shows what fraction of your tokens went to each model. A project that sends most tokens to Opus when Sonnet would suffice costs roughly 1.5–5× more than necessary. If you see unexpected Opus usage, check whether agent subagent routing or explicit model selection is the cause.

---

## CLI commands

```
python cli.py scan [--projects-dir PATH]
    Scan ~/.claude/projects/ (or a custom path) for new/updated JSONL files
    and update the database. Safe to run repeatedly — only processes changes.

python cli.py today
    Print today's token usage and estimated cost broken down by model.

python cli.py week
    Print the last 7 days: a per-day summary and a per-model breakdown.

python cli.py stats
    Print all-time statistics: sessions, token totals, cost by model,
    top projects by usage, and a 30-day daily average.

python cli.py dashboard [--projects-dir PATH] [--host HOST] [--port PORT]
    Run a scan, then start the local web dashboard at http://localhost:8080.
    Opens a browser tab automatically. Press Ctrl+C to stop.

python cli.py rates
    Show the current effective pricing for all models (defaults + overrides).

python cli.py set-rate --model MODEL --input N --output N --cache-read N --cache-write N
    Override the pricing for one model (all rates in $ per 1M tokens).
    The override is saved to ~/.claude/claude_usage_rates.json and takes
    effect immediately on the next dashboard request.
```

---

## Dashboard layout

### Filter bar
- **Model checkboxes** — toggle individual model versions in/out of all charts and tables. Default: all billable models selected (any model containing "opus", "sonnet", or "haiku").
- **Range buttons** — This Week, This Month, Prev Month, 7d, 30d, 90d, All Time.
- **Custom date range** — enter a specific start and/or end date to override the range buttons.

### Overview cards
Seven summary cards for the filtered time range: Sessions, Turns, Input Tokens, Output Tokens, Cache Read, Cache Creation, Estimated Cost.

### Projects
Collapsible accordion, sorted by estimated cost (highest first). Each project shows:
- **Header** — project name, session count, turn count, date range, estimated cost.
- **Sessions table** — one row per session with session ID, date, duration, model, turns, input, output, cache read, cache creation, and estimated cost. A totals row appears at the bottom.
- **Model donut chart** — token share by exact model version for that project's sessions.

### Global daily chart
A stacked bar chart covering the full filtered date range. Bars are segmented by token type (input, output, cache read, cache creation). A cost line overlay on the right axis shows the estimated API cost per day.

---

## Data files

| Path | Purpose |
|------|---------|
| `~/.claude/projects/**/*.jsonl` | Raw Claude Code session transcripts (source of truth) |
| `~/.claude/usage.db` | SQLite database built by the scanner |
| `~/.claude/claude_usage_rates.json` | Per-model rate overrides (created by `set-rate`) |
