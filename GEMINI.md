# Botsensai Workspace Guardrails & Live Trading Rules

## 1. Strict Isolation of Live-Money Trades vs. Simulation Data
* **Zero Paper Data in Live Reports**: When operating in live trading mode, status reports, PnL summaries, and user updates MUST NEVER include simulated, backtested, or paper trading records.
* **On-Chain Truth Invariant**: Report only verified on-chain cryptographic signatures (`tx_hash`), confirmed atomic Jito bundle landings, and actual hot wallet SOL balance movements.
* **Epoch Filtering**: Any queries to `paper_positions` or execution records must strictly filter by `LIVE_START_TS` (timestamp `>= 1790165760.0`) to exclude pre-live simulation artifacts.

## 2. Hard Capital Preservation Rails
* **Hot Wallet Reserve Floor**: Never allow the hot wallet balance to drop below `0.10 SOL`. If balance approaches the floor, halt new entries immediately while maintaining exit monitoring.
* **Position Sizing Ceiling**: Hard cap single trade allocation at `0.025 SOL` max (default `0.010 - 0.017 SOL`).
* **Slippage & Priority Protection**: Enforce a strict `1.5%` (150 bps) max slippage ceiling and capped Jito MEV tips (500k–800k lamports) to eliminate front-running and capital bleed.

## 3. Dual-Pool Execution & Migration Fallback
* **Pre/Post-Graduation Routing**: All swap transaction builders must handle both pre-migration Pump.fun bonding curves (`pool: "pump"`) and post-migration Raydium AMM pools (`pool: "pump-amm"`).
* **Automatic 400 Recovery**: If a swap returns a curve completion error or HTTP 400 on `pool: "pump"`, the execution engine must automatically re-route and retry via `pool: "pump-amm"`.

## 4. Remote Control & AWS SSM Execution Context
* **Environment Traversal**: Scripts and SSM commands must target `/home/ubuntu/Botsensai` and run within the active virtualenv (`/home/ubuntu/Botsensai/.venv/bin/python3`).
* **Multi-User Keypair Resolution**: The hot wallet keypair loader must explicitly resolve `/home/ubuntu/.config/solana/id.json` to function reliably under both `ubuntu` and `root` SSM invocation contexts.
* **Shell Safety**: When dispatching remote SSM commands, avoid unescaped shell expressions (e.g. `$`, `f-strings`, `<` inside double quotes); prefer dedicated script execution or clean base64 payload dispatch.
