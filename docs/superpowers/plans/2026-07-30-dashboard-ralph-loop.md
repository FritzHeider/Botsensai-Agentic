# Loop task: implement the static dashboard and collection-integrity panel

You are implementing ONE plan, task by task. You have no memory of previous
iterations. Everything you need is on disk.

## Read these first, every iteration

- `docs/superpowers/plans/2026-07-30-dashboard-static-and-integrity.md` — **the
  plan**. It contains six tasks, each with exact file paths, the test code, the
  implementation code, and the commands to run. Follow it literally.
- `docs/superpowers/specs/2026-07-29-botsensai-dashboard-design.md` — why the
  design is the way it is. Read this if a plan step seems arbitrary.
- `git log --oneline -10` — what previous iterations actually did.

Ignore `@fix_plan.md` for this run. Its queue is Phase 1 work and is not what
this loop is for. The one exception is Task 6 Step 8, which updates the P5-01
entry; do that when you reach it.

## Iteration rules

- Work **one task** per iteration — the first whose checkboxes are not all
  ticked. Do not start a second task in the same iteration.
- Tick each `- [ ]` checkbox to `- [x]` in the plan file as you complete that
  step. That file is the progress record; a fresh iteration reads it to find
  where to resume.
- The plan's code blocks are the intended implementation. If you deviate, say so
  in the commit message and explain why.
- End every iteration with a commit. Never leave uncommitted work.
- State specifically what changed: which task, which files, which tests, and the
  actual numbers the commands printed. Identical output across iterations trips
  loop detection and kills the run.
- If a task is genuinely blocked, write the blocker into the plan file under
  that task, commit, and stop. Do not retry a failing command in a loop.

## Hard constraints

These come from `PROMPT.md` and hold for every iteration:

- **Never add signing or transaction-submission code.**
  `tests/test_scoring_and_execution.py::test_repository_contains_no_signing_code`
  enforces this and must keep passing.
- **Never commit a key, keyfile path, API token, or `.env`.** This plan
  introduces no secret at all — the static build must contain none.
- **Never return `0.0` from a metric to mean "no data",** and never render
  MISSING as a number. Absence is not a bearish claim.
- **Never delete or weaken a failing test to make the suite pass.** If a test is
  genuinely wrong, fix it and explain why in the commit message.
- **Add no dependencies.** `jinja2` and `httpx` are already present; serving
  later uses stdlib. Do not add a package.
- **No external assets in the generated HTML.** No CDN, no remote font, no
  remote image. Inline all CSS.
- Naming: the substrings `x_password`, `twitter_password`, `def login(`,
  `fill_login`, `type_password` must not appear anywhere under `src/botsensai`.
- Line length 100, enforced by ruff.

## Acceptance criteria

Run these; do not assert them. All must pass before an iteration ends.

- `python -m pytest -q` exits 0.
- `python -m ruff check src tests` exits 0.
- `python -c "from botsensai.metrics import build_registry; r=build_registry(); assert len(r) >= 32, len(r)"` exits 0.

Once Task 6 is complete, additionally:

- `python -m botsensai.cli dashboard --out /tmp/dash.html && python -c "import pathlib; h=pathlib.Path('/tmp/dash.html').read_text(); assert '<html' in h and len(h)>5000"` exits 0.
- `grep -c "http://\|https://" /tmp/dash.html` finds nothing (exit 1).

## Scope

Only the six tasks in the plan. Served mode, the Gemini chatbot, action buttons
and the job runner are **out of scope** — they are separate plans. Do not start
a server, read an API key, or send data anywhere.

## Done

The run is complete when every checkbox in the plan file is ticked, all
acceptance commands above exit 0, and the work is committed.

TASK_COMPLETE
