# Task: Build Botsensai 2.0 — Advanced Usability, Interactive Visuals, Web-Use Scraping & Generative Multimodal Content

You are working on **Botsensai**, an agentic Solana memecoin recon, scoring, backtesting, paper-trading, and multimodal content intelligence system.

The core pipeline (34 anti-adversarial signals, point-in-time SQLite WAL store, walk-forward embargoed backtester, coordinate ascent weight fitter, ablation engine, paper broker with curve impact and sandwich penalties) is fully verified and passing.

Your job is to execute **Botsensai 2.0** as specified in `@fix_plan.md` (Phases 6 through 9), implementing 20 major usability, visual, and analytical superpowers in the most sophisticated manner possible.

---

## Read these first, every iteration

- `@fix_plan.md` — The task queue. Work the first unchecked task whose dependencies are satisfied. Nothing else.
- `docs/ARCHITECTURE.md` — System architecture and strict layer dependencies.
- `docs/DATA_SOURCES.md` — Verified endpoints, field names, and rate limits.
- `docs/superpowers/specs/2026-09-01-botsensai-2.0-comprehensive-spec.md` — Complete technical specifications for Botsensai 2.0 features.
- `git log --oneline -15` — What recent iterations actually did.

You have no memory of previous iterations. Everything you need is on disk.

---

## Core Requirements & Integrations

- **Python 3.11+**, managed with `pyproject.toml`.
- **Web-Use Skill (Playwright)**:
  - Used by collectors (`src/botsensai/collectors/browser.py`, `src/botsensai/media/gallery.py`) to scrape dynamic DEX interfaces (Dexscreener, pump.fun, Meteora), extract DOM snapshots, harvest tweet images, and parse social threads safely.
  - Must run stealth, handle rate limits gracefully, support headless/headed modes, and never crash a sweep on page timeout (`degraded=True`).
- **Fal.ai Multimodal Generation**:
  - Integrated into `src/botsensai/media/fal_cards.py` and `src/botsensai/media/generator.py` using the `fal-client` library.
  - Generates high-resolution 1200x675 social summary infographics, visual radar cards, and AI-assisted meme lineage analysis.
  - Strict fallback: If Fal.ai credentials are unset or the service is offline, gracefully fall back to local SVG/Pillow rendering with explicit degraded logging.
- **FastAPI + WebSockets Backend**:
  - Lightweight async server for real-time web UI (`botsensai ui`) and headless API (`botsensai serve`).
  - No heavyweight dependencies (no Postgres, Redis, or Celery) — use SQLite WAL mode with asyncio broadcast queues.
- **Strict Invariants**:
  - **No Signing Code**: Never add transaction-signing or live wallet execution code. `tests/test_scoring_and_execution.py::test_repository_contains_no_signing_code` must always pass.
  - **Absence is Never Bearishness**: Metrics with missing data return `MISSING` (`None`), never `0.0`.
  - **Point-in-Time Correctness**: Every query used by backtesting, wallet skill indices, and AI copilots must filter on both `as_of <= decision_time` and `observed_at <= decision_time`.
  - **Mandatory Disclosure**: All generated content and shareable cards must include automated disclaimer watermarks.

---

## Acceptance Criteria

Every iteration must satisfy the following backpressure checklist before committing:

1. `python scripts/backpressure.py` exits 0 (9/9 gates green, max complexity <= 10, zero type errors).
2. `python -m pytest -q` exits 0 with zero failures.
3. `python -m ruff check src tests scripts` exits 0.
4. `python -m mypy src` exits 0.
5. The specific acceptance command for the active task in `@fix_plan.md` exits 0.

---

## Iteration Rules

1. Work **one** task from `@fix_plan.md` per iteration — the first unchecked task whose dependencies are satisfied.
2. If a task is marked under `**IN PROGRESS**`, complete that before picking anything new.
3. If a task exceeds one iteration context, split it into atomic sub-tasks in `@fix_plan.md`, commit the split, and finish the first sub-task.
4. Write thorough unit and integration tests in `tests/` for every newly introduced module.
5. In your iteration output, report:
   - Task ID completed (e.g. `P6-01`).
   - Files created / modified.
   - Tests added and exact test counts.
   - Exact numerical output of the acceptance command.
6. Run `git add -A && git commit` before concluding the iteration. Never leave uncommitted work on disk.

---

## Signs & Guardrails

- **A live web dashboard must not block the discovery pipeline.** Use an async event pump or non-blocking in-memory queue.
- **Fal.ai calls must be asynchronous and bounded by a timeout.** Wrap all external AI calls in `asyncio.wait_for(..., timeout=30.0)` with local SVG fallbacks.
- **Interactive TUI must cleanly release the terminal.** Ensure Textual / curses cleanup handles `Ctrl+C` and terminal resize signals without corrupting stdout.
- **AI Copilot responses must cite stored evidence.** Any LLM response summarizing a token must include exact metric IDs and values retrieved from the database.
- **Playwright instances must be pooled and recycled.** Prevent browser memory leaks by reusing browser contexts and enforcing max page counts.

---

## Status

- [ ] All Phase 6 tasks checked off (Visual Interfaces & Real-Time Exploration)
- [ ] All Phase 7 tasks checked off (Interactive Intelligence & Multimodal Media)
- [ ] All Phase 8 tasks checked off (Analytical Depth & Quantitative Tooling)
- [ ] All Phase 9 tasks checked off (Developer Experience & Operational Polish)
- [ ] `python scripts/backpressure.py` passes with 9/9 green gates
- [ ] LOOP_COMPLETE
