# Botsensai dashboard — design

**Date:** 2026-07-29
**Status:** awaiting review
**Extends:** `@fix_plan.md` P5-01 — its static artifact and acceptance test are
preserved exactly; this adds a served mode alongside them

---

## Why this document is cautious

`@fix_plan.md` ends with a warning against the thing this spec describes:

> Do not skip to phase 5. A dashboard over an unvalidated signal is a very
> convincing way to lose money.

That warning is correct and the operator has read it. The weights are unfitted
priors, there are zero labelled outcomes, usable metric coverage sits near 36%
against a `min_coverage` floor of 0.5, and the only backtests that have run are
over synthetic data.

A session on 2026-07-29 found **five** independent bugs in the collection path,
all of which presented as a healthy system with no error logged anywhere:

| bug | effect |
|---|---|
| tweet walker capped at depth 10 (tweets sit at 12–14) | 0 posts parsed from 128 KB of real tweets |
| enrich aborted at 100s; a headed session pass needs ~6 min | nothing stored |
| `token_key` never passed to `insert_posts` | 494 posts stored complete and unreachable |
| X moved user fields out of `legacy` | every author read `"unknown"`, manufacturing a shill signal at high confidence |
| `clamp()` applied to entropy in nats | two metrics flattened to near-constants |

Two of those did not hide data — they **invented** it. This shapes the design
more than any aesthetic consideration: a dashboard renders whatever the pipeline
claims, and would have displayed a confident 0.147 author-diversity score for
every token while looking entirely plausible. The primary job of this dashboard
is therefore **to make silent wrongness visible**, and only secondarily to
present scores.

---

## Goals

1. Show current candidates, their scores, and the exact reason each was refused.
2. Make collection integrity legible enough that a bug of the five kinds above
   is visible without reading the database by hand.
3. Cover the CLI feature set with real controls rather than documentation.
4. Answer "why did this token score that?" in prose, grounded in real queries.
5. Never present an unvalidated number as more settled than it is.

## Non-goals

- Any write path to trading configuration. No kill switch, no editable risk
  limits (that is P5-04, and it is deliberately out of scope).
- P&L, equity curves, or track record. Zero labelled outcomes exist; a
  performance chart over an empty `outcomes` table would be theatre.
- Remote or multi-user access. This is a local operator tool.
- Live trading anything. `LiveBroker` raises and this changes nothing about that.

---

## Decisions

| decision | choice | rationale |
|---|---|---|
| Delivery | **Two modes** from one codebase | `dashboard --out FILE` keeps P5-01's static self-contained artifact and its acceptance test; `dashboard --serve` adds chat and controls as progressive enhancement |
| Action surface | **Reads execute, writes are jobs** | cheap reads are instant; `sweep`/`backtest` are tracked background jobs with streamed logs, because a sweep hits eight live APIs under rate limits and now takes ~190s |
| Chat grounding | **Gemini function calling** | the model requests exactly what it needs, execution stays local, and every claim traces to a real query rather than a pre-baked summary |
| Layout | **Command deck** | left rail for the full command set, content centre, chat docked right so explanation sits beside evidence |
| Visual register | **Terminal instrument** | dark default, monospace numerics, hairline rules, accent colour reserved for state; dense numeric tables scan better and it reads as an instrument, not a pitch |

### Dependencies: none added

`jinja2` and `httpx` are already in `pyproject.toml`. Serving uses stdlib
`http.server.ThreadingHTTPServer`. No FastAPI, no uvicorn, no npm, no bundler —
which is also what keeps P5-01's "no build step" true.

---

## Architecture

New package `src/botsensai/dashboard/`:

| module | responsibility |
|---|---|
| `data.py` | Read-only snapshot builder over `Database` + metric registry + settings. Returns plain dicts. **Single source of truth for both modes.** |
| `integrity.py` | Collection-integrity checks (see below). Pure functions over a snapshot. |
| `render.py` | Jinja2 → HTML. `static` mode inlines CSS/JS and omits chat; `serve` mode links them. |
| `tools.py` | The read-only callable surface, shared by buttons and by the chat's function declarations. One definition, two consumers. |
| `jobs.py` | Background jobs: spawns `botsensai <cmd>` as a subprocess from a fixed allowlist, captures output, tracks status, supports cancel. |
| `gemini.py` | Gemini REST via httpx; function-calling loop. |
| `server.py` | Loopback HTTP server, static assets, JSON endpoints. |

Data flow, serve mode:

```
browser → server.py → tools.py → data.py → Database (read-only)
                    → jobs.py  → subprocess botsensai <cmd>
                    → gemini.py → Google API   (only path leaving the machine)
```

Static mode is `data.py → render.py → one .html file`. No server, no key, no chat.

`tools.py` existing as one layer is deliberate: the chat and the buttons must not
be able to disagree about what the data says.

---

## Panels

Candidates (composite, coverage, veto reasons) · **collection integrity** ·
metric coverage by family · collector health from `doctor` · sweep freshness ·
memory notes · weights version · X-session state · job console.

### Collection integrity panel

This is the panel that exists because of the five bugs, and the one to build
first. Each check is a number that would have been visibly wrong:

| check | would have caught |
|---|---|
| posts stored vs posts **reachable** (`token_key` set) | bug 3 — 494 stored, 0 reachable |
| distinct authors ÷ posts, per platform | bug 4 — 390 posts, 1 distinct author |
| % posts with `author_created_at`, `views`, `bookmarks` | bug 4 — 0/390 on all three |
| per-surface enrich outcome: ok / degraded / **timed out** | bug 2 — X killed at 100s every sweep |
| posts parsed per captured GraphQL payload | bug 1 — payloads captured, 0 parsed |
| distinct raw values per metric across tokens | bug 5 — one value repeated for every token |

That last check generalises: **a metric returning the same raw value for every
token is reporting a constant, not a signal.** It is cheap to compute and is the
single highest-value alarm in the system.

### Honesty rules — enforced in `render.py`, not by template discipline

- `MISSING` renders as the string `MISSING` plus its reason. Never `0.0`, never
  an empty bar. Returning zero for unknown is the bug `README.md` names as
  poisoning composites.
- Permanent, non-dismissible header: `paper · UNVALIDATED · N labelled outcomes`.
- Any backtest figure carries `SYNTHETIC` inline, never in a footnote.
- Scores written **before** 2026-07-29 are marked `PRE-FIX — contaminated`.
  Bug 4 penalised every token for fabricated author concentration, so the
  historical rows are not a baseline and must not read as one.
- Empty panels state why and print the command that would populate them.

Charts are hand-rolled inline SVG; no external assets are permitted. Load the
`dataviz` skill before writing any chart code.

---

## Chatbot

Seven function declarations, all read-only, all executing locally against
`tools.py`: `list_candidates`, `get_score`, `get_metric_values`,
`explain_metric`, `coverage_summary`, `collector_health`, `list_memory`.

- **Key:** `GEMINI_API_KEY` from environment only. Never in config, never in the
  served HTML, never in the static file — matching the repo's existing "secrets
  come from the environment only" posture. Absent key → panel renders disabled
  with the reason.
- **Model:** configurable, default `gemini-2.5-flash`. If the API rejects the
  name, surface the error *and* the models the API reports as available rather
  than failing opaquely.
- **Transparency:** every function call and result appears in the transcript, so
  what left the machine is always visible.
- **Egress consent:** a first-run panel naming exactly what gets sent — token
  symbols, scores, metric values, registry thesis text. This is the only
  component that transmits operator data to a third party.
- **Guardrails:** system instruction forbids price predictions and forbids
  asserting anything absent from a function result, mirroring
  `media/generator.py`'s existing refusals. It must also be willing to say the
  data is too thin to answer — with 36% coverage that is often the true answer.
- **Naming constraint:** `test_no_credential_field_exists_anywhere` bans the
  substrings `x_password`, `twitter_password`, `def login(`, `fill_login`,
  `type_password` anywhere under `src/botsensai`. Nothing here may be named
  `login`.

---

## Action surface

Buttons map to a **fixed allowlist of argv arrays**. No command string ever
arrives from the request; no shell is invoked.

| command | mode |
|---|---|
| `doctor`, `metrics`, `explain`, `score`, `x-session`, `memory`, `weights`, `recap` | immediate |
| `sweep`, `backtest` | background job, streamed log, cancellable |
| `remember` | form → job |
| `x-setup` | **excluded** — interactive login flow; links to `docs/X_SESSION.md` |

Job logs poll at 500 ms. No WebSockets.

---

## Security

- Binds `127.0.0.1`. A non-loopback `--host` requires explicit `--allow-remote`
  and prints a warning.
- All POSTs require a per-process token generated at startup and injected into
  the page. A loopback server that executes commands is otherwise reachable by
  any page in the browser via CSRF.
- The static artifact contains no key, no chat, and no external references — it
  is safe to share, which is the point of P5-01.

## Error handling

- Panel with no data → "no data", the reason, and the command that fixes it.
- Job failure → exit code and stderr tail, verbatim.
- Gemini failure (no key, 4xx, 5xx, quota) → rendered inline in the chat, never
  as a fabricated answer.
- A `data.py` query that raises must fail that panel only, not the page — the
  same partial-results-beat-no-results rule the collector base class follows.

## Testing

- P5-01's acceptance command passes verbatim.
- Static build: contains `<html`, exceeds 5000 chars, contains no chat markup,
  no API key, and no `http://` asset references.
- `MISSING` never renders as a number.
- Integrity checks: each of the six flags fires on a fixture reproducing its bug.
  These are regression tests for tonight's five bugs at the presentation layer.
- Gemini tool loop unit-tested against a stubbed transport, asserting **no
  network access in tests** and that the key is read from env only.
- Allowlist: no endpoint executes a string; `x-setup` is absent.
- Existing 125 tests keep passing; the credential-substring guard stays clean.

---

## Risks and open questions

1. **The dashboard makes thin data look authoritative.** Mitigated by the
   permanent banner, `MISSING` rendering, and `PRE-FIX` marking — but mitigation
   is not elimination, and this remains the main argument against building now.
2. **Gemini will confabulate over sparse data.** The function-call transcript
   makes it checkable, and the system instruction tells it to refuse. Neither is
   a guarantee.
3. **Model id may be stale.** Handled by surfacing the API's own model list on
   rejection.
4. **Static and serve modes could drift.** Mitigated by both rendering from the
   same `data.py` snapshot and the same template.
5. **Unresolved:** whether the integrity panel should refuse to render scores at
   all when a metric reports a constant across every token. Currently specified
   as a loud flag rather than a refusal. Worth revisiting once there is more than
   one sweep of post-fix data to look at.

## Not building

Kill switch and editable risk limits (P5-04) · WebSockets · P&L or track record
panels · authentication beyond the loopback token · any write path to trading
configuration · a `dashboard` entry in the sweep loop (regeneration on every
sweep is P5-01's concern and can follow once this exists).
