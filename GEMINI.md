# Botsensai Workspace Guardrails & Live Trading Rules

## 1. Strict Isolation of Live-Money Trades vs. Simulation Data
* **Zero Paper Data in Live Reports**: When operating in live trading mode, status reports, PnL summaries, and user updates MUST NEVER include simulated, backtested, or paper trading records.
* **On-Chain Truth Invariant**: Report only verified on-chain cryptographic signatures (`tx_hash`), confirmed atomic Jito bundle landings, and actual hot wallet SOL balance movements.
* **Epoch Filtering**: Any queries to `paper_positions` or execution records must strictly filter by `LIVE_START_TS` (timestamp `>= 1790165760.0`) to exclude pre-live simulation artifacts.

## 2. Hard Capital Preservation Rails
* **Hot Wallet Reserve Floor**: Never allow the hot wallet balance to drop below `0.10 SOL`. If balance approaches the floor, halt new entries immediately while maintaining exit monitoring.
* **Position Sizing Ceiling**: Hard cap single trade allocation at `0.035 SOL` max (dynamic 5% bankroll scaling above reserve floor, default `0.012 - 0.025 SOL`).
* **Slippage & Priority Protection**: Enforce a strict `3.5%` (350 bps) max slippage ceiling for fast meme runner entries and tiered Jito MEV tips (550k–1,200,000 lamports tiered by conviction score) to guarantee transaction inclusion while eliminating front-running and capital bleed.

## 3. Dual-Pool Execution & Multi-Pool Liquidity Fallback
* **Pre/Post-Graduation Routing**: All swap transaction builders must handle pre-migration Pump.fun bonding curves (`pool: "pump"`), post-migration Raydium AMM pools (`pool: "pump-amm"`), standard Raydium (`pool: "raydium"`), concentrated pools (`pool: "raydium-cpmm"`), and automated smart routing (`pool: "auto"`).
* **Automatic 400 Recovery**: If a swap returns a curve completion error or HTTP 400 on `pool: "pump"`, the execution engine must automatically cascade and retry across `pump-amm`, `raydium`, and `auto` to eliminate dropped trades.

## 4. Remote Control & AWS SSM Execution Context
* **Environment Traversal**: Scripts and SSM commands must target `/home/ubuntu/Botsensai` and run within the active virtualenv (`/home/ubuntu/Botsensai/.venv/bin/python3`).
* **Multi-User Keypair Resolution**: The hot wallet keypair loader must explicitly resolve `/home/ubuntu/.config/solana/id.json` to function reliably under both `ubuntu` and `root` SSM invocation contexts.
* **Shell Safety**: When dispatching remote SSM commands, avoid unescaped shell expressions (e.g. `$`, `f-strings`, `<` inside double quotes); prefer dedicated script execution or clean base64 payload dispatch.
