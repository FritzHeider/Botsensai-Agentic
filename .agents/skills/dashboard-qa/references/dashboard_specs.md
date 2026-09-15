# Botsensai Dashboard Specifications & DOM Contracts

This document defines the architectural specifications, DOM selectors, CSS tokens, and verification contracts for Botsensai web dashboard interfaces (`dash.HTML` and files rendered via `botsensai dashboard`).

---

## 1. Core Principles

1. **Strict Airgap & Self-Contained Integrity**:
   - The dashboard must function completely offline with zero external CDNs, fonts, or tracking scripts.
   - Any link to `http://` or `https://` is treated as a security/isolation violation by the QA engine unless explicitly whitelisted.
2. **Deterministic Layout**:
   - Monospace typography (`ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`).
   - Dark glassmorphic color palette defined via CSS custom properties on `:root`.

---

## 2. CSS Design Tokens (`:root`)

| Variable | Default Hex | Semantic Meaning |
| :--- | :--- | :--- |
| `--bg` | `#0b0d10` | Canvas background |
| `--panel` | `#12151a` | Card & section background |
| `--line` | `#232830` | Borders, dividers, table lines |
| `--text` | `#d7dde5` | Primary body text |
| `--dim` | `#7d8794` | Secondary text, labels, column headers |
| `--ok` | `#4a9d6a` | Healthy state / passing status dot |
| `--warn` | `#c08a3e` | Degraded state / warning status dot |
| `--alarm` | `#c0553e` | Failed state / critical integrity alarm |
| `--accent` | `#5b8bb5` | Progress bar fill & visual highlights |

---

## 3. DOM Hierarchy & Selector Contracts

### Header (`<header>`)
* **Brand**: `header .brand` — Displays uppercase brand name (`BOTSENSAI`).
* **Mode Banner**: `header .banner` — Displays execution mode (`paper · UNVALIDATED`).
* **Timestamp**: `header .dim` — Generation timestamp in UTC (`generated YYYY-MM-DD HH:MM:SSZ`).

### Main Content Grid (`<main>`)
* Grid definition: `grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr)`.
* Responsive breakpoint: Single column under 900px (`@media(max-width:900px)`).

### Candidate Table (`section table`)
* **Table Headers (`th`)**: Uppercase, tabular text (`num` class right-aligned).
* **Veto Indication**: `.veto` class rendered with `--alarm` text color.
* **Tag Badges**: `.tag` (dim border) and `.tag.pre` (warn border).
* **Metric Fill Bars**: `.bar` (container with `--line` background) containing `i` (absolute positioned element filled with `--accent`).

### Integrity Flags (`.flag`)
* **Status Dot**: `.dot` (8x8 rounded indicator).
  * `.dot.ok` -> green
  * `.dot.warn` -> yellow/orange
  * `.dot.alarm` -> red
* **Headline**: `.flag .h` (font-weight: 600).
* **Detail**: `.flag .d` (color: `--dim`).

---

## 4. API Endpoints Contract (`src/botsensai/api.py`)

* `GET /docs` — Swagger UI documentation page (HTTP 200).
* `GET /api/v1/regime` — JSON containing `trading_mode`, `weights_version`, and database table counts.
* `GET /api/v1/metrics` — JSON array of all 34 registered adversarial signals.
* `GET /api/v1/candidates?limit=N` — Recent scored candidate tokens with composite and coverage scores.
* `GET /api/v1/tokens/{mint}/score` — Detailed breakdown for a specific token mint.
