import re

# 1. Patch pipeline.py
with open("/home/ubuntu/Botsensai/src/botsensai/pipeline.py", "r") as f:
    pipe = f.read()

# Add copytrade import if not present
if "from botsensai.copytrade.tracker import CopyTradeTracker" not in pipe:
    pipe = "from botsensai.copytrade.tracker import CopyTradeTracker\n" + pipe

# In ScoringPipeline.__init__
if "self.copy_tracker = CopyTradeTracker(self.db)" not in pipe:
    pipe = pipe.replace(
        "self._consecutive_degraded_sweeps: int = 0",
        "self._consecutive_degraded_sweeps: int = 0\n        self.copy_tracker = CopyTradeTracker(self.db)\n        self._sweep_counter: int = 0"
    )

# In decide(): add copy trade detection and conviction boost
old_decide_smart = """        # Check for real-time smart money participation boost
        trades = self.db.trades_as_of(launch.token.key, result.as_of)"""

new_decide_smart = """        # Check for real-time smart money and followed top trader early entries
        trades = self.db.trades_as_of(launch.token.key, result.as_of)
        launch_ts = (
            launch.created_at.timestamp()
            if hasattr(launch.created_at, "timestamp")
            else float(launch.created_at)
        )
        copy_alerts = self.copy_tracker.check_copy_trade(
            token_key=launch.token.key,
            symbol=launch.token.symbol,
            mint=launch.token.mint,
            launch_created_at=launch_ts,
            trades=trades,
        )
        if copy_alerts:
            copy_boost = max(a.conviction_boost for a in copy_alerts)
            result.composite = min(1.0, result.composite + copy_boost)
            best_alert = max(copy_alerts, key=lambda a: a.conviction_boost)
            result.explanation = (
                (result.explanation or "")
                + f" [COPY TRADE: Followed top trader {best_alert.trader_wallet[:8]}... "
                f"(skill {best_alert.trader_skill:.2f}, win rate {best_alert.trader_win_rate:.0%}) "
                f"entered early with {best_alert.trader_buy_sol:.2f} SOL (+{copy_boost:.2f} boost)]"
            )"""

if old_decide_smart in pipe and "copy_alerts = self.copy_tracker" not in pipe:
    pipe = pipe.replace(old_decide_smart, new_decide_smart)

# Record copy trade events
old_upsert = """        self.db.upsert_paper_position(
            token_key=launch.token.key,
            symbol=launch.token.symbol,
            mint=launch.token.mint,
            amount_token=fill.amount_token,
            cost_basis_native=fill.amount_native + fill.fee_native + fill.tip_native,
            entry_price_native=fill.price_native,
            peak_price_native=fill.price_native,
            last_price_native=fill.price_native,
            opened_at=fill.as_of,
            status="OPEN",
        )"""

new_upsert = """        self.db.upsert_paper_position(
            token_key=launch.token.key,
            symbol=launch.token.symbol,
            mint=launch.token.mint,
            amount_token=fill.amount_token,
            cost_basis_native=fill.amount_native + fill.fee_native + fill.tip_native,
            entry_price_native=fill.price_native,
            peak_price_native=fill.price_native,
            last_price_native=fill.price_native,
            opened_at=fill.as_of,
            status="OPEN",
        )
        if copy_alerts:
            for alert in copy_alerts:
                self.copy_tracker.record_copy_event(alert)"""

if old_upsert in pipe and "record_copy_event" not in pipe:
    pipe = pipe.replace(old_upsert, new_upsert)

# Upgrade _manage_open_positions
old_manage = """    def _manage_open_positions(self, report: SweepReport) -> None:
        \"\"\"Mark and exit anything already open against the freshest snapshot.\"\"\"
        for key in list(self.broker.account.positions.keys()):
            snapshots = self.db.snapshots_as_of(key, utcnow())
            if not snapshots:
                continue
            latest = snapshots[-1]
            position = self.broker.account.positions[key]
            self.broker.mark(position.token, latest.price_native or 0.0)
            fills = self.broker.apply_exits(position.token, latest, utcnow())
            report.exited += sum(1 for f in fills if not f.rejected)"""

new_manage = """    def _manage_open_positions(self, report: SweepReport) -> None:
        \"\"\"Mark and exit anything open against freshest snapshot and DB state.\"\"\"
        now = utcnow()
        now_ts = now.timestamp()

        # 1. Manage in-memory broker positions
        for key in list(self.broker.account.positions.keys()):
            position = self.broker.account.positions.get(key)
            if position is None or not position.is_open:
                continue

            snapshots = self.db.snapshots_as_of(key, now)
            if snapshots:
                latest = snapshots[-1]
                self.broker.mark(position.token, latest.price_native or 0.0)
                fills = self.broker.apply_exits(position.token, latest, now)
                for f in fills:
                    if not f.rejected:
                        report.exited += 1
                        self.db.close_paper_position(
                            key,
                            exit_price_native=f.price_native,
                            reason=f.reason or "exit",
                        )
            else:
                age_sec = (now - position.opened_at).total_seconds()
                if age_sec >= self.settings.risk.max_hold_seconds:
                    exit_px = position.last_price_native or (
                        position.cost_basis_native / max(1e-6, position.amount_token)
                    )
                    self.broker.close_position(
                        position.token, exit_px, now, reason="time_stop_max_hold"
                    )
                    report.exited += 1
                    self.db.close_paper_position(
                        key, exit_price_native=exit_px, reason="time_stop_max_hold"
                    )

        # 2. Reconcile DB open_paper_positions to guarantee no orphaned positions
        try:
            db_open = self.db.open_paper_positions()
            for pos in db_open:
                tok_key = pos["token_key"]
                age_sec = max(0.0, now_ts - pos["opened_at"])
                if age_sec >= self.settings.risk.max_hold_seconds:
                    exit_px = pos.get("last_price_native") or pos.get("entry_price_native", 0.0)
                    self.db.close_paper_position(
                        tok_key, exit_price_native=exit_px, reason="time_stop_max_hold"
                    )
                    if tok_key in self.broker.account.positions:
                        p = self.broker.account.positions[tok_key]
                        if p.is_open:
                            self.broker.close_position(
                                p.token, exit_px, now, reason="time_stop_max_hold"
                            )
                    report.exited += 1
                    log.info(
                        "paper.auto_reconciled_time_stop",
                        token=tok_key,
                        age_hours=round(age_sec / 3600, 2),
                    )
        except Exception as exc:
            log.warning("paper.reconcile_failed", error=str(exc))"""

if old_manage in pipe:
    pipe = pipe.replace(old_manage, new_manage)

# In sweep(): periodic refresh
old_sweep_start = """        candidates = self.screen(discovered.launches, max_candidates)"""
new_sweep_start = """        if getattr(self, "_sweep_counter", 0) % 50 == 0:
            try:
                self.copy_tracker.refresh_top_traders(max_candidates=100)
            except Exception as exc:
                log.warning("copytrade.refresh_failed", error=str(exc))
        self._sweep_counter = getattr(self, "_sweep_counter", 0) + 1

        candidates = self.screen(discovered.launches, max_candidates)"""

if old_sweep_start in pipe and "copy_tracker.refresh_top_traders" not in pipe:
    pipe = pipe.replace(old_sweep_start, new_sweep_start)

with open("/home/ubuntu/Botsensai/src/botsensai/pipeline.py", "w") as f:
    f.write(pipe)
print("pipeline.py patched successfully!")

# 2. Patch api.py
with open("/home/ubuntu/Botsensai/src/botsensai/api.py", "r") as f:
    api_code = f.read()

copytrade_endpoints = """
@router.get("/copytrade/top_traders")
async def get_copytrade_top_traders(
    request: Request,
    limit: int = 50,
) -> dict[str, Any]:
    \"\"\"Return ranked roster of top consistent on-chain winning traders.\"\"\"
    settings = _get_active_settings(request)
    db = Database(settings.path(settings.db_path))
    try:
        from botsensai.copytrade.tracker import CopyTradeTracker
        tracker = CopyTradeTracker(db)
        traders = tracker.get_top_traders(limit=limit)
        return {
            "total_tracked": len(traders),
            "top_traders": traders,
        }
    finally:
        db.close()


@router.get("/copytrade/events")
async def get_copytrade_events(
    request: Request,
    limit: int = 50,
) -> dict[str, Any]:
    \"\"\"Return recent copy-trade detection events and early conviction alerts.\"\"\"
    settings = _get_active_settings(request)
    db = Database(settings.path(settings.db_path))
    try:
        with db.conn as conn:
            import sqlite3
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                \"\"\"
                SELECT id, token_key, symbol, mint, trader_wallet, trader_skill,
                       trader_win_rate, trader_buy_sol, entry_delay_seconds,
                       conviction_boost, recommended_size_sol, signal_id, created_at
                FROM copy_trade_events
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                \"\"\",
                (limit,),
            ).fetchall()
            return {
                "total_events": len(rows),
                "events": [dict(r) for r in rows],
            }
    finally:
        db.close()
"""

if "/copytrade/top_traders" not in api_code:
    target = "def create_headless_api_app"
    api_code = api_code.replace(target, copytrade_endpoints + "\n\n" + target)
    with open("/home/ubuntu/Botsensai/src/botsensai/api.py", "w") as f:
        f.write(api_code)
    print("api.py patched successfully!")
else:
    print("api.py already has copytrade endpoints.")
