"""Atomic private bundle builder and multi-region Jito endpoint manager.

Constructs atomic transactions bundled with ComputeBudget instructions and Jito tips,
protecting orders from front-running, back-running, and toxic sandwich attacks.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from botsensai.config import Settings, get_settings
from botsensai.execution.jito_tips import JITO_TIP_ACCOUNTS, JitoTipEngine
from botsensai.util.logging import get_logger

log = get_logger(__name__)

# Primary multi-region Jito Block Engine endpoints
JITO_REGIONAL_BLOCK_ENGINES = [
    "https://ny.mainnet.block-engine.jito.wtf",
    "https://slc.mainnet.block-engine.jito.wtf",
    "https://frankfurt.mainnet.block-engine.jito.wtf",
    "https://amsterdam.mainnet.block-engine.jito.wtf",
    "https://tokyo.mainnet.block-engine.jito.wtf",
]


@dataclass
class AtomicBundleSpec:
    bundle_id: str
    target_mint: str
    amount_sol: float
    tip_lamports: int
    tip_account: str
    anti_sandwich: bool
    endpoints: list[str]


class BundleBuilder:
    """Constructs sandwich-proof atomic bundle specifications."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.tip_engine = JitoTipEngine.get_instance(self.settings)

    def select_random_tip_account(self) -> str:
        """Randomly distribute tips across valid Jito tip accounts to prevent hot accounts."""
        return random.choice(JITO_TIP_ACCOUNTS)

    def build_atomic_bundle(
        self,
        token_mint: str,
        amount_sol: float,
        conviction_score: float = 0.70,
        is_contended: bool = False,
    ) -> AtomicBundleSpec:
        """Create an atomic bundle spec with dynamic tip and anti-sandwich protection."""
        tip_lamports = self.tip_engine.calculate_tip_lamports(
            conviction_score=conviction_score,
            trade_size_sol=amount_sol,
            is_contended=is_contended,
        )
        tip_account = self.select_random_tip_account()
        anti_sandwich = self.settings.execution.anti_sandwich_private_bundle

        # Select primary local endpoint (Ashburn prefers NY/SLC)
        endpoints = [
            self.settings.jito_block_engine_url or "https://ny.mainnet.block-engine.jito.wtf",
            "https://slc.mainnet.block-engine.jito.wtf",
            "https://frankfurt.mainnet.block-engine.jito.wtf",
        ]

        import uuid

        return AtomicBundleSpec(
            bundle_id=uuid.uuid4().hex[:16],
            target_mint=token_mint,
            amount_sol=amount_sol,
            tip_lamports=tip_lamports,
            tip_account=tip_account,
            anti_sandwich=anti_sandwich,
            endpoints=endpoints,
        )
