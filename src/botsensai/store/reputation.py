"""Deployer and wallet reputation registry.

Tracks blacklisted serial ruggers and whitelisted top-tier creators.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from botsensai.config import Settings, get_settings
from botsensai.models import VetoReason
from botsensai.util.logging import get_logger

log = get_logger(__name__)


class ReputationRegistry:
    """Registry maintaining deployer blacklists and whitelists."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        settings = get_settings()
        self.db_path = str(db_path or settings.path(settings.db_path))
        self._blacklist: set[str] = set()
        self._whitelist: set[str] = set()
        self._init_tables()
        self._load_cache()

    def _init_tables(self) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS deployer_reputation (
                        wallet TEXT PRIMARY KEY,
                        status TEXT NOT NULL,  -- 'blacklist' | 'whitelist'
                        reason TEXT,
                        rug_count INTEGER DEFAULT 0,
                        success_count INTEGER DEFAULT 0,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.commit()
        except Exception as err:
            log.debug("reputation.init_failed", error=str(err))

    def _load_cache(self) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT wallet, status FROM deployer_reputation")
                for wallet, status in cur.fetchall():
                    if status == "blacklist":
                        self._blacklist.add(wallet)
                    elif status == "whitelist":
                        self._whitelist.add(wallet)
        except Exception as err:
            log.debug("reputation.load_failed", error=str(err))

    def is_blacklisted(self, wallet: str) -> bool:
        return wallet in self._blacklist

    def is_whitelisted(self, wallet: str) -> bool:
        return wallet in self._whitelist

    def add_blacklist(self, wallet: str, reason: str = "serial_rugger") -> None:
        self._blacklist.add(wallet)
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO deployer_reputation (wallet, status, reason, rug_count)
                    VALUES (?, 'blacklist', ?, 1)
                    ON CONFLICT(wallet) DO UPDATE SET
                        status = 'blacklist',
                        rug_count = rug_count + 1,
                        reason = excluded.reason,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (wallet, reason),
                )
                conn.commit()
        except Exception as err:
            log.debug("reputation.record_failed", error=str(err))

    def check_deployer(self, wallet: str) -> VetoReason | None:
        """Return DEPLOYER_PRIOR_RUGS if wallet is in active blacklist."""
        if self.is_blacklisted(wallet):
            log.warning("reputation.deployer_blacklisted", wallet=wallet)
            return VetoReason.DEPLOYER_PRIOR_RUGS
        return None
