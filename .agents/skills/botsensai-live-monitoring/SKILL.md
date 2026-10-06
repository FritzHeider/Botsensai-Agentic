---
name: botsensai-live-monitoring
description: >-
  Live wallet status monitoring, sweep log inspection via AWS SSM, Helius 429
  congestion window handling, and AWS session management for Botsensai running
  on EC2. Use whenever checking live wallet balance, monitoring pipeline sweep
  health, diagnosing Helius RPC degradation, or restoring monitoring after AWS
  session expiry.
---

# Botsensai Live Monitoring Skill

Operational runbook for monitoring the Botsensai trading bot running on
EC2 instance `i-0bb5f0e7d264a2937` (us-east-1, IP 54.227.221.254).

---

## Infrastructure Reference

| Item | Value |
|------|-------|
| EC2 instance | `i-0bb5f0e7d264a2937` (us-east-1) |
| Hot wallet | `ChgCuBWDvGFwnW77kU523JcFXc2CryX3J4rEWDzWmtsS` |
| Gas floor | 0.010 SOL (halt entries if buffer < 0.003 SOL) |
| Repo path | `/home/ubuntu/Botsensai` |
| Daemon log | `/home/ubuntu/Botsensai/data/daemon.log` |
| AWS profile | `agent-profile` |
| Wallet check script | `/Users/drop/.gemini/antigravity/brain/c78c9ffd-41c9-4444-b7be-c09f31e7c6a9/scratch/wallet_status.py` |

---

## 1. Standard 5-Minute Check Pattern

Run wallet balance check and sweep log pull in parallel:

```bash
python3 /Users/drop/.gemini/antigravity/brain/c78c9ffd-41c9-4444-b7be-c09f31e7c6a9/scratch/wallet_status.py &
python3 -c "
import subprocess, json, time, re
INSTANCE='i-0bb5f0e7d264a2937'; REGION='us-east-1'
r = subprocess.run(['aws','ssm','send-command','--profile','agent-profile',
    '--instance-ids',INSTANCE,'--region',REGION,
    '--document-name','AWS-RunShellScript',
    '--parameters','commands=[\"cd /home/ubuntu/Botsensai && grep pipeline.sweep data/daemon.log | tail -4\"]',
    '--query','Command.CommandId','--output','text'],capture_output=True,text=True)
cid = r.stdout.strip()
if cid and len(cid) == 36:
    time.sleep(18)
    o = subprocess.run(['aws','ssm','get-command-invocation','--profile','agent-profile',
        '--command-id',cid,'--instance-id',INSTANCE,'--region',REGION,'--output','json'],
        capture_output=True,text=True)
    if o.stdout.strip():
        d = json.loads(o.stdout); out = d.get('StandardOutputContent','')
        for m in re.finditer(r'(\d{2}:\d{2}:\d{2}).*?degraded_surfaces=(\[.*?\]).*?enriched=(\d+).*?entered=(\d+)', out):
            tag = '✅' if m.group(2) == '[]' else '⚠️'
            print(f'{tag} {m.group(1)} degraded={m.group(2)} enriched={m.group(3)} entered={m.group(4)}')
else:
    print('AWS session expired — run: aws login --profile agent-profile')
" &
wait
```

**Key notes:**
- `time.sleep(18)` is required — SSM execution takes 12–18 seconds
- Use `--parameters 'commands=[\"...\"]'` CLI form, NOT the JSON dict form, to avoid dash-shell escaping issues on EC2
- `wallet_status.py` uses direct Solana RPC and works even when the AWS session is expired
- When using `run_command` tool with `WaitMsBeforeAsync=28000` to catch both outputs in one go

---

## 2. Sweep Log Interpretation

Each sweep log line format:
```
HH:MM:SS ... pipeline.sweep ... degraded_surfaces=[...] enriched=N entered=N
```

| Status | Meaning |
|--------|---------|
| `✅ degraded=[] enriched=10 entered=0` | Perfect sweep — all surfaces healthy, no qualifying entries |
| `⚠️ degraded=['enrich'] enriched=10 entered=0` | Helius 429 — enrichment degraded, correctly capped by coverage floor guard |
| `⚠️ degraded=['enrich'] enriched=8 entered=0` | Helius 429 + partial token enrichment failure — still correctly capped |
| `entered=N` where N > 0 | Bot entered N positions — verify on-chain balance change |

**Safety invariant**: Any sweep with `degraded=['enrich']` is safe — the coverage floor guard
(fix #3) blocks score inflation when coverage ≤ 0.25, preventing entries on incomplete data.

---

## 3. Helius 429 Congestion Windows (Known Pattern)

Two daily pressure windows observed consistently (US Eastern time):

| Window | UTC | ET | Duration | Pattern |
|--------|-----|----|----------|---------|
| US Market Open | 13:35–14:04 | 09:35–10:04 AM | ~30 min | Sustained consecutive degraded sweeps |
| Midday / Lunch | 15:42–16:12 | 11:42 AM–12:12 PM | ~30 min | Sustained consecutive degraded sweeps |

**During these windows:**
- Expect 4–8 consecutive `degraded=['enrich']` sweeps
- Capital is NOT at risk — coverage floor guard blocks all entries during degraded sweeps
- SSM monitoring itself may also slow / timeout during peak congestion
- Recovery is automatic — Helius self-heals; no manual intervention needed

**Root cause**: `base.py:265` fires `asyncio.gather(*(c.run_enrich(tokens) for c in collectors))`
— all enrichment collectors fire simultaneously, creating an RPC burst against the Helius DAS API.

---

## 4. AWS SSM Session Management

### Detecting Session Expiry
SSM `send-command` returns empty stdout when the session is expired:
```python
cid = r.stdout.strip()
if not cid or len(cid) != 36:
    # stderr contains: "Your session has expired. Please reauthenticate"
    print('AWS session expired — run: aws login --profile agent-profile')
```

### Graceful Degradation
When AWS session is expired:
- `wallet_status.py` still works (uses Solana mainnet RPC directly, not AWS)
- On-chain balance and open positions are still verifiable
- Sweep log monitoring is unavailable until re-auth
- Report balance only and note SSM is down

### Restoring the Session
```bash
aws login --profile agent-profile
# Opens browser for AWS SSO authentication (~60 seconds)
# SSM automatically works again on next check
```

### Verifying Restoration
If `cid` is a valid 36-char UUID after `send-command`, SSM is restored.

---

## 5. Active Cron Tasks

| Task | Schedule | Purpose |
|------|----------|---------|
| 5-min wallet cron | `*/5 * * * *` | Automated wallet + sweep check reports |
| 6h top-wallet reseed | every 6h | Re-seeds `top_traders` table from Pump.fun API |

---

## 6. Capital Safety Checklist (per check)

- [ ] Balance ≥ 0.010 SOL gas floor (halt entries if buffer < 0.003 SOL above floor)
- [ ] Open positions = 0 or verified on-chain positions only
- [ ] No unexplained balance decrease > 0.035 SOL (max position size)
- [ ] Daemon log shows recent sweep timestamps (within ~5 min of check time)

---

## 7. Escalation Triggers

Investigate daemon + positions immediately if:
- Balance drops by any amount while `entered=0` in all recent sweeps
- `entered > 0` appears in sweep log for the first time this session
- Sweep timestamps stop updating (daemon may have crashed — check `systemctl status botsensai`)
- `enriched=0` persists across multiple consecutive sweeps (collector failure, not 429)
- `enriched < 5` without `degraded=['enrich']` (unexpected enrichment failure)
