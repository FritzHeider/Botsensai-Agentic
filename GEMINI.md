# Botsensai Workspace Guardrails & Live Trading Rules

## 1. Strict Isolation of Live-Money Trades vs. Simulation Data
* **Zero Paper Data in Live Reports**: When operating in live trading mode, status reports, PnL summaries, and user updates MUST NEVER include simulated, backtested, or paper trading records.
* **On-Chain Truth Invariant**: Report only verified on-chain cryptographic signatures (`tx_hash`), confirmed atomic Jito bundle landings, and actual hot wallet SOL balance movements.
* **Epoch Filtering**: Any queries to `paper_positions` or execution records must strictly filter by `LIVE_START_TS` (timestamp `>= 1790165760.0`) to exclude pre-live simulation artifacts.

## 2. Hard Capital Preservation Rails
* **Operational Hot Wallet Gas Floor**: Maintain an operational gas and rent reserve floor of `0.010 SOL` (preserving ATA rent exemption ~0.00204 SOL, network fees, and Jito tips to prevent wallet bricking). If balance approaches the `0.010 SOL` floor (buffer < 0.003 SOL), halt new entries immediately while maintaining exit monitoring.
* **Position Sizing & Active Trading**: Deploy remaining liquid SOL in the hot wallet into active trades sized at `0.015 - 0.025 SOL` (hard cap `0.035 SOL` max, dynamic bankroll scaling based on available buffer above the gas floor).
* **Autonomous Sentinel Pre-Trade Gate**: All candidate buy signals must be audited by the Autonomous Sentinel Agent (`BotsensaiSentinelAgent` / DAS on-chain security inspection) before swap generation to eliminate weaponized Token-2022 authorities, phishing dust, honeypots, and illiquid traps.
* **Slippage & Priority Protection**: Enforce a strict `3.5%` (350 bps) max slippage ceiling for fast meme runner entries and tiered Jito MEV tips (550k–1,200,000 lamports tiered by conviction score) to guarantee transaction inclusion while eliminating front-running and capital bleed.

## 3. Dual-Pool Execution & Multi-Pool Liquidity Fallback
* **Pre/Post-Graduation Routing**: All swap transaction builders must handle pre-migration Pump.fun bonding curves (`pool: "pump"`), post-migration Raydium AMM pools (`pool: "pump-amm"`), standard Raydium (`pool: "raydium"`), concentrated pools (`pool: "raydium-cpmm"`), and automated smart routing (`pool: "auto"`).
* **Automatic 400 Recovery**: If a swap returns a curve completion error or HTTP 400 on `pool: "pump"`, the execution engine must automatically cascade and retry across `pump-amm`, `raydium`, and `auto` to eliminate dropped trades.

## 4. Remote Control & AWS SSM Execution Context
* **Environment Traversal**: Scripts and SSM commands must target `/home/ubuntu/Botsensai` and run within the active virtualenv (`/home/ubuntu/Botsensai/.venv/bin/python3`).
* **Multi-User Keypair Resolution**: The hot wallet keypair loader must explicitly resolve `/home/ubuntu/.config/solana/id.json` to function reliably under both `ubuntu` and `root` SSM invocation contexts.
* **Shell Safety**: When dispatching remote SSM commands, avoid unescaped shell expressions (e.g. `$`, `f-strings`, `<` inside double quotes); prefer dedicated script execution or clean base64 payload dispatch.

## 5. Anti-Phishing, Malicious Dust & Token-2022 Authority Guardrails
* **Zero Third-Party Wrapper Routing**: Never route swaps, RPC queries, or WebSocket feeds through unverified third-party wrapper APIs (e.g. `pumpdev.io`, `pumpapi.io`, or unverified middleman proxies). All Solana communication must strictly interface with dedicated enterprise Helius RPC (`mainnet.helius-rpc.com`), official Jito Block Engines (`mainnet.block-engine.jito.wtf`), and native on-chain program accounts.
* **Malicious Dust & Token-2022 Airdrop Quarantining**: Unsolicited tokens airdropped to the hot wallet (promotional spam tokens, vanity ad tokens like `SWITCH TO PUMPAPI.IO...`, or tokens containing Token-2022 `permanent_delegate` / `freeze_authority` owned by untrusted third parties) must be classified as zero-value `DUST` and quarantined. Never approve transactions, attempt to trade, or interact with dangerous delegated authorities.
* **Metadata & Authority Verification**: When auditing holdings or analyzing suspicious candidate mints, inspect token extensions and metadata (using DAS `getAsset`) to verify that mint/freeze authorities and permanent delegates are not weaponized against the wallet.

