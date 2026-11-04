# AegisForge — instructions for Claude Code

docs/SPEC.md is the source of truth. If something conflicts with it, is ambiguous,
or looks wrong, STOP and ask. Do not improvise architecture. If any `<FILL>` remains
in SPEC.md §2, ask the owner before starting.

## Stack
Python 3.12, uv, ruff, mypy --strict (gateway/, harness/), pytest, typer.
Model IDs come only from config/models.toml. Never hardcode keys; read from env.

## Hard rules
- No DB credentials, API keys, MCP servers, or Docker socket inside the sandbox.
  The sandbox gets exactly one credential: test_runner for run_<id>.
- Security decisions are deterministic code (L0/L1). LLM components may only escalate.
- Never remove tools from the tool list mid-run; deny at execution with ToolDenied.
- Context is append-only; the static prefix must stay byte-identical across turns.
- Every security invariant (SPEC §6.1) has a test in tests/security/.
- Before using FastMCP proxying, cedarpy, AgentDojo, Prompt Guard, or the SWE-bench
  harness, read their current docs/source and state the API you found.
- No new dependencies beyond SPEC §2 without asking.
- NEVER run `git commit` or `git push` autonomously. Only the human owner commits.

## Workflow (Strict Step-by-Step Control)
1. Work strictly on the specific scope or step requested in the user prompt (do NOT attempt an entire week at once unless explicitly asked).
2. Plan mode: list files to create/touch, design decisions, and how acceptance criteria will be checked. Wait for user approval before writing code.
3. Implement changes only for the agreed step.
4. Run checks (`ruff`, `mypy`, relevant tests).
5. STOP and present the changes (`git status` / `git diff` summary).
6. Wait for human inspection and explicit instruction before touching the next file or step.