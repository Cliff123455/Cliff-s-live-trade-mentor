# .antigravity/rules.md — ScalpBot Project Constitution

> **Read this before writing a single line of code.**
> This file is the law of the repo. Antigravity agents, Claude Code, and any other AI
> assistant working in this project must follow every rule here. When in doubt, be
> conservative, ask, and document.

---

## 🧭 Who This Repo Belongs To

This is Cliff's private project — a multi-agent scalping bot (ScalpBot / CliffClaw).
It is forked from Nvidia's NeMo but is now an independent codebase under Cliff's GitHub account.
Do not submit pull requests to Nvidia's original repo unless Cliff explicitly asks.
Do not push to any remote without Cliff's express permission when doing so, always Ask if he'd like to create a new branch and a pull request. 

---

## 🌿 Branch Strategy

### The Golden Rule

> **`main` is always deployable. Nothing broken lives on `main`.**

### Branch Types

| Branch prefix  | When to use                              | Example                            |
|----------------|------------------------------------------|------------------------------------|
| `feature/`     | New agent, new capability, new tool      | `feature/position-monitor-agent`   |
| `fix/`         | Bug fix in existing code                 | `fix/redis-race-condition`         |
| `refactor/`    | Restructuring without changing behavior  | `refactor/coordinator-synthesis`   |
| `config/`      | Config file changes only                 | `config/update-risk-params`        |
| `docs/`        | Documentation and spec updates           | `docs/update-agents-md`            |
| `experiment/`  | Trying something new, may be thrown away | `experiment/langraph-integration`  |

### Rules

- Never commit directly to `main` — always work on a branch.
- Branch names are lowercase with hyphens only — no spaces, no underscores, no capitals.
- Delete branches after they are merged. Keep the repo clean.
- One concern per branch. Don't mix a feature and a bug fix in the same branch.

---

## 📝 Commit Message Convention

### Format

```
<type>(<scope>): <short summary>

[optional body — what changed and why]

[optional footer — e.g. Closes #12, BREAKING CHANGE: ...]
```

### Types

| Type       | Meaning                                             |
|------------|-----------------------------------------------------|
| `feat`     | A new feature or agent capability                   |
| `fix`      | A bug fix                                           |
| `refactor` | Code change that doesn't fix a bug or add a feature |
| `config`   | Changes to config files (yaml, env, etc.)           |
| `docs`     | Documentation only                                  |
| `test`     | Adding or updating tests                            |
| `chore`    | Housekeeping — dependencies, CI, tooling            |

### Examples

```
feat(risk-agent): add daily drawdown halt at -3% account

Implements the emergency halt logic. On breach, publishes to
scalpbot:risk:daily-halt and sets state:risk-params.halted = true.
Position monitor subscribes and closes all open positions immediately.

Closes #7
```

```
fix(coordinator): prevent duplicate directives on signal replay

Idempotency check on sequence_id was missing. Added set-based
dedup in synthesis.py. Fixes race condition where fast tape prints
caused the same setup to be scored twice within 100ms.
```

### Rules

- Summary line: 50 characters max, present tense, no period at end.
- Body: wrap at 72 characters. Explain why, not just what.
- Never commit secrets. No API keys, no broker passwords, no `.env` values.
- If you're not sure what changed or why, don't commit yet — figure it out first.

---

## 🔒 What Never Goes in Git

The `.gitignore` should already cover these, but agents must never attempt to commit:

```
.env
.env.*
config/secrets.yaml
*.key
*.pem
broker_credentials.*
logs/
__pycache__/
*.pyc
.DS_Store
artifacts/trades/      ← trade logs stay local only
artifacts/sessions/    ← session summaries stay local only
```

If you find credentials anywhere in the codebase: stop, do not commit, tell Cliff.

---

## 🧪 Testing Requirements

### Before Any Merge to `main`

- [ ] `pytest tests/ -v` passes with zero failures
- [ ] The risk veto test passes: `pytest tests/test_risk_veto.py -v`
- [ ] The execution split test passes: `pytest tests/test_execution_split.py -v`
- [ ] No new code is added without at least one corresponding test
- [ ] No test is deleted without an explanation in the PR description

### Test File Naming

- Test files live in `tests/`
- Named `test_<module_name>.py` — mirrors the source file it covers
- Example: `src/agents/risk_modeling_agent.py` → `tests/test_risk_modeling_agent.py`

### What to Test

- **Risk Agent**: every veto condition, every sizing calculation, daily halt trigger
- **Coordinator**: signal synthesis logic, conviction threshold, abstention cases
- **Execution agents**: missed entry handling, directive_id validation, veto cancellation
- **Performance Agent**: weight update formula, minimum sample size enforcement

---

## 🤖 Agent Collaboration Rules

These rules apply to every AI agent (Antigravity, Claude Code, etc.) working in this repo.

### ⚠️ THE GOLDEN RULE: ASK CLIFF BEFORE CHANGING ANYTHING

**No AI agent may modify code, config, UI, or architecture without asking Cliff first.**

Multiple AI agents work on this codebase simultaneously. Uncoordinated changes
cause conflicts, regressions, and wasted time. Every agent must:

1. **Propose** what it wants to change and why.
2. **Wait** for Cliff to approve.
3. **Then** make the change.

This applies to ALL changes — code, config values, UI controls, dependencies,
git operations, and architecture. The only exception is when Cliff has explicitly
approved a specific task in the current conversation. Even then, stick to the
approved scope — no "while I'm here" improvements.

### Explain Every Command Before Running It

**When running any shell/bash/terminal command, always tell Cliff what it does
in plain English before executing.** Format: "This command does [one sentence]."
No silent execution. No assuming Cliff knows what a command does. If chaining
commands, explain each one. If running something destructive, flag it clearly.

**Do not silently change config values** (thresholds, feature flags, tuning
parameters). If you think a value should change, explain why and ask first.

**Do not remove UI controls.** Cliff uses dashboard inputs during live trading.

> This rule is also documented in `CLAUDE.md` at the repo root. Both files
> must stay in sync.

### Always Do

- Read `AGENTS.md` before touching any agent code — it's the communication contract spec.
- Work on a branch. Never touch `main` directly.
- Write tests alongside code — not after the fact.
- Update `AGENTS.md` if you change a message channel, payload field, or state store key.
- Log decisions to `artifacts/decisions/` when making architectural choices.
- Ask Cliff before making changes that affect more than one agent tier.

### Never Do

- Never override or work around the Risk Agent's veto logic — it is inviolable.
- Never add a new broker API call outside of `order-execution-agent` or `position-monitor-agent`.
- Never push directly to `main` or `origin/main`.
- Never delete or modify trade logs in `artifacts/trades/` — they are append-only.
- Never hardcode config values that belong in `config/*.yaml`.
- Never ignore a failing test and merge anyway.
- Never create a new agent without a corresponding spec section in `AGENTS.md`.

### When You're Unsure

> Stop. Write a comment in the code explaining what you were trying to do and why you stopped.
> Create a `docs/` branch, document the uncertainty, and flag it for Cliff to review.
> Better to pause than to break something in production.

---

## 🔁 Pull Request Checklist

Before opening a PR to `main`, verify every item:

- [ ] Branch name follows the naming convention above
- [ ] All commits follow the message format above
- [ ] `pytest tests/ -v` passes locally
- [ ] No secrets or credentials in any changed file
- [ ] `AGENTS.md` is updated if any communication contracts changed
- [ ] PR description explains what changed and why
- [ ] No unrelated changes snuck into this branch

### PR Description Template

```
## What changed
Brief description of the change.

## Why
The reason this change was needed.

## How to test
Steps to verify this works.

## Checklist
- [ ] Tests pass
- [ ] No secrets committed
- [ ] AGENTS.md updated if needed
```

---

## 📁 Where Things Live

```
CliffClaw/
├── .antigravity/
│   └── rules.md           ← THIS FILE — read first
├── AGENTS.md              ← Agent communication contracts — read second
├── config/                ← All tunable parameters (no secrets here)
├── src/agents/            ← One file per agent
├── src/coordinator/       ← Signal synthesis logic
├── src/execution/         ← Broker routing
├── src/memory/            ← Weight update formula
├── tests/                 ← All tests — mirrors src/ structure
└── artifacts/             ← Decision logs, trade logs (gitignored)
```

---

## 🆘 If Something Goes Wrong

- Don't panic and don't force push. That makes recovery harder.
- If you broke `main` somehow — open an issue immediately, describe what happened.
- If an agent pushed bad code — revert the commit with `git revert <hash>`, don't delete history.
- If broker credentials were accidentally committed — rotate them immediately, then clean git history with `git filter-branch` or BFG Repo Cleaner.
- When in doubt: `git stash`, step back, re-read this file.

---

## 🏁 Definition of Done

A task is done when:

- Code works and tests pass
- Branch is merged to `main` via a PR
- Branch is deleted after merge
- `AGENTS.md` reflects any spec changes
- `artifacts/decisions/` has a log entry if an architectural decision was made
- Cliff has reviewed and approved the PR

---

*This file is a living document. Update it when the project evolves — but always on a `docs/` branch, never directly on `main`.*

Last updated: 2026-03-23 — ScalpBot / CliffClaw v1.0
