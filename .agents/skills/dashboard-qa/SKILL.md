---
name: dashboard-qa
description: >-
  Automated end-to-end testing, visual QA, and DOM integrity verification for Botsensai's
  web dashboard (dash.HTML / generated dashboards), D3 topology graphs, and headless API streams.
  Use this skill whenever verifying web UI changes, testing dashboard generation, checking
  for JavaScript console errors, or auditing client-side integrity metrics.
---

# Dashboard QA Skill

This skill provides automated headless browser verification and visual quality assurance for Botsensai's web surfaces:
1. **Static Self-Contained Dashboards**: `dash.HTML` in the repo root or files generated via `botsensai dashboard --out <path>`.
2. **Interactive Live Server**: Web dashboard and REST/WebSocket API served via `botsensai serve --port 8001`.
3. **Visual Regression & DOM Integrity**: Ensures zero external HTTP/CDN leaks, valid numeric formatting, intact integrity indicator dots, and zero JavaScript runtime exceptions.

---

## Quickstart Commands

### 1. Audit the Static Dashboard Snapshot (`dash.HTML`)
```bash
python3 .agents/skills/dashboard-qa/scripts/run_qa.py --target dash.HTML --screenshot /tmp/botsensai-dashboard.png
```

### 2. Generate a Fresh Dashboard from Database and Test It
```bash
# 1. Generate fresh snapshot from data/botsensai.db
botsensai dashboard --out /tmp/dash_fresh.html

# 2. Run automated headless browser QA
python3 .agents/skills/dashboard-qa/scripts/run_qa.py --target /tmp/dash_fresh.html
```

### 3. Test Live API & WebSocket Streams (When Daemon or Server is Running)
```bash
python3 .agents/skills/dashboard-qa/scripts/test_api_stream.py --port 8001
```

---

## What the QA Suite Verifies

| Check Category | Verification Details | Failure Criteria |
| :--- | :--- | :--- |
| **Page Hydration** | Valid HTML5 doctype, UTF-8 charset, responsive viewport, `<title>` contains "botsensai". | Page fails to load or wrong title. |
| **Layout & Structure** | Header brand, mode banner, generation timestamp, split grid, section headers. | Missing header, missing candidate section. |
| **Data Integrity** | Table rows render candidate mints, scores, tags, and progress bars. | Empty candidate table when store has data. |
| **Status Dots** | Integrity indicator dots (`.dot.ok`, `.dot.warn`, `.dot.alarm`) have valid CSS colors. | Malformed flags or missing status indicators. |
| **Security / Airgap** | Completely self-contained; zero unbundled `http://` or `https://` script/font requests. | External network leak detected. |
| **Console Errors** | Intercepts all browser `console.error` events and unhandled JS exceptions. | Any unhandled JavaScript error or rejected promise. |
| **Visual Artifact** | Captures high-resolution full-page screenshot. | Failure to render or capture image. |

---

## File Structure

```text
.agents/skills/dashboard-qa/
├── SKILL.md                          # This runbook
├── scripts/
│   ├── run_qa.py                     # Headless Playwright DOM & visual validation engine
│   └── test_api_stream.py            # API endpoint and WebSocket health verification
└── references/
    └── dashboard_specs.md            # DOM selector contracts, integrity alarms, and CSS specifications
```

---

## Step-by-Step QA Workflow for Agents

When requested to verify or update the web dashboard:
1. **Run the QA Engine**:
   Execute `python3 .agents/skills/dashboard-qa/scripts/run_qa.py --target dash.HTML`.
2. **Check the Output Summary**:
   Confirm all checks pass:
   - `[PASS] DOM structure and headers`
   - `[PASS] Table and candidate rows`
   - `[PASS] Integrity flags and alarm indicators`
   - `[PASS] Zero console errors`
   - `[PASS] Zero external network requests`
3. **Inspect Screenshot**:
   If visual layout changes were made, view or embed the screenshot saved at `/tmp/botsensai-dashboard.png`.
4. **If Failures Occur**:
   Refer to [dashboard_specs.md](./references/dashboard_specs.md) to inspect expected CSS classnames and selector hierarchies.
