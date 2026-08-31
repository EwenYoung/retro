<div align="right">

[中文](README.md) · **English**

</div>

<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="retro — compile agent session experience into persistent knowledge, then distill it into reusable rules. The ledger lines show the three layers: log excerpts, structured entries, and the resident AGENTS.md rule with an applied ok mark.">
</p>

---

## Features

<p align="center">
  <img src="./assets/readme/section-features.svg" width="100%" alt="Features section header">
</p>

| Feature | Description |
|---|---|
| Session Capture | At wrap-up, the agent reviews the session and writes reusable pitfalls and strategies into the project's `.retro/` knowledge base. Append-only — the log is the single source of truth |
| Verified Gating | Promoting an entry to the resident rule section in `AGENTS.md` requires evidence: hit again (seen_count≥2) or actually applied and worked (applied_ok≥1) |
| Two-Tier Memory | `.retro/` is an on-demand archive with no size limit; `AGENTS.md` is the always-loaded front stage capped at 12 rules. Entries flow both ways |
| Deterministic Bookkeeping | Counting, dedup, promotion/demotion, and audit logs are all handled by the script with zero LLM involvement — the LLM only judges and writes content |
| Audit Rounds | The `audit` subcommand produces a six-block read-only report: health / promote candidates / demote candidates / stale candidates / merge candidates / audit status |
| Dismissal Memory | Candidates dismissed in an audit stay silent for 7 days, then resurface flagged "please re-review" — never nagging, never lost |

## Quick Start

```bash
git clone https://github.com/EwenYoung/retro.git ~/.agents/skills/retro
```

That's it — everything after installation is handled automatically by the agent. No configuration needed.

## Usage

<p align="center">
  <img src="./assets/readme/section-usage.svg" width="100%" alt="Usage section header: tell the agent to retro">
</p>

Variants like "distill this session" or "note today's pitfalls" also work. Say nothing and the agent will still proactively suggest a retro after a substantial task — a tough bug fix, an environment setup, a long debugging session. When there is nothing worth recording, it says so instead of padding.

### What the Agent Does Behind the Scenes

Once triggered, the agent:

1. **Reviews the session** for five signal types: failed attempts, user corrections, hard-to-find information, overturned assumptions, and reliably effective strategies
2. **Applies the inclusion bar**: reusable, non-obvious, valid across sessions — all three must hold. Content read from existing entries does not count as new experience (keeps statistics clean)
3. **Writes to `.retro/log/`** (raw excerpts, append-only) and `.retro/entries/` (structured entries), recording `seen-again` for recurring pitfalls. When a past lesson is actually used during work, the agent judges on its own whether it worked and records `applied: ok/fail` — lessons are consulted automatically during real work, no user prompting needed
4. **Runs the bookkeeping script**: `retro.py index` fills derived fields, `retro.py check` validates (0 errors is the acceptance line)
5. **Decides on promotion**: `retro.py escalate` lists candidates with per-item reasoning; after your confirmation, `--apply` promotes entries into the `AGENTS.md` rule section (capped at 12; oldest gets demoted first when full)

The promotion bar is hard: an entry must be `verified` and either seen at least twice or applied successfully at least once. Unproven entries stay in the archive.

### Periodic Audits

When the last audit is ≥7 days old, ≥10 new entries have accumulated, or the rule section is ≥10/12 full, the agent suggests an audit at wrap-up. It runs `retro.py audit` to get a six-block read-only report, reviews each candidate semantically, and hands you a decision list — promote, demote, merge, or dismiss. Nothing executes without your confirmation. Finally, `audit --close` records the round. Dismissed candidates stay silent for 7 days, then resurface flagged for re-review.

## Architecture

<p align="center">
  <img src="./assets/readme/section-architecture.svg" width="100%" alt="Architecture section header">
</p>

<p align="center">
  <img src="./assets/readme/architecture.svg" width="100%" alt="retro architecture: session experience is captured into the log, compiled into entries by index, and promoted to resident AGENTS.md rules through verified gating. The audit loop on the right drives promotion (green), demotion (red), and dismissal (gray) verdicts, closing each round into the jsonl logs.">
</p>

- **Downward flow (capture)**: session → log → entries → AGENTS.md, with a bar at every step
- **Upward flow (feedback)**: `applied ok/fail` lines record how lessons perform in real use, driving the next round of promotion/staleness decisions
- **The loop (audit)**: periodic audits review the whole base — verified lessons get promoted, stale ones demoted, dismissals are remembered

## Project Structure

```
retro/
├── SKILL.md                   # Skill instructions: review signals, inclusion bar, audit workflow
├── scripts/
│   └── retro.py               # Deterministic script: index / check / stats / escalate / reconcile / audit
├── tests/
│   └── test_retro.py          # 24 integration tests
├── assets/readme/             # README visual assets (SVG sources)
├── LICENSE
└── README.md
```

Data lives in your project, not in this repository:

```
<project>/
├── AGENTS.md                  # Rules section (retro-managed block, ≤12 rules)
└── .retro/
    ├── log/YYYY-MM-DD.md      # Raw session excerpts (append-only)
    ├── entries/YYYYMMDD-NNN.md  # Structured entries (YAML frontmatter + body)
    ├── INDEX.md               # Script-generated index
    ├── escalation.log.jsonl   # Promotion/demotion audit log
    └── audit.log.jsonl        # Audit round records
```

## License

[MIT](LICENSE)
