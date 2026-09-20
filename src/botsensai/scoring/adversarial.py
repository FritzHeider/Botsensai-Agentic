"""Adversarial alpha filters: Dev bundler sybil detection and 2-hop CEX funding graph traversal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from botsensai.config import Settings, get_settings
from botsensai.models import SecurityReport, VetoReason
from botsensai.util.logging import get_logger

log = get_logger(__name__)

# Known CEX hot wallets and instant exchange bridges frequently abused by serial deployers
KNOWN_CEX_AND_BRIDGE_ROOTS = {
    "FixedFloatBridge1111111111111111111111111",
    "ChangeNOWInstantSwap111111111111111111111",
    "BinanceHotWalletSolana1111111111111111111",
    "KucoinHotWalletSolana11111111111111111111",
    "MEXCHotWalletSolana1111111111111111111111",
}


@dataclass
class BundlerAnalysis:
    is_dev_bundle: bool
    bundle_wallet_count: int
    bundle_supply_fraction: float
    shared_funder: str | None
    veto: VetoReason | None


class AdversarialDetector:
    """Detects slot-0 dev bundler sybils and insider cabals linked via common funding roots."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def inspect_slot0_bundle(
        self,
        security: SecurityReport | None,
        raw_bundle_transfers: list[dict[str, Any]] | None = None,
    ) -> BundlerAnalysis:
        """Evaluate if the launch was pre-sniped by the developer in an atomic sybil bundle."""
        if not security:
            return BundlerAnalysis(
                is_dev_bundle=False,
                bundle_wallet_count=0,
                bundle_supply_fraction=0.0,
                shared_funder=None,
                veto=None,
            )

        # Check bundle supply share from security report
        bundle_share = getattr(security, "bundled_share", None) or 0.0
        insider_share = getattr(security, "insider_share", None) or 0.0
        max_bundle = self.settings.scoring.veto_dev_bundle_share

        if bundle_share >= max_bundle or (bundle_share >= 0.20 and insider_share >= 0.20):
            log.warning(
                "adversarial.dev_bundle_sybil_detected",
                bundle_share=bundle_share,
                insider_share=insider_share,
                threshold=max_bundle,
            )
            return BundlerAnalysis(
                is_dev_bundle=True,
                bundle_wallet_count=getattr(security, "bundle_wallets_count", 4) or 4,
                bundle_supply_fraction=bundle_share,
                shared_funder=None,
                veto=VetoReason.DEV_BUNDLER_SYBIL,
            )

        return BundlerAnalysis(
            is_dev_bundle=False,
            bundle_wallet_count=getattr(security, "bundle_wallets_count", 0) or 0,
            bundle_supply_fraction=bundle_share,
            shared_funder=None,
            veto=None,
        )

    def check_2hop_funding_cabal(
        self,
        deployer_funder: str | None,
        early_buyer_funders: list[str] | None,
    ) -> VetoReason | None:
        """Inspect 2-hop funding graph to detect shared burner roots between dev and early buyers."""
        if not self.settings.scoring.veto_cex_insider:
            return None
        if not deployer_funder or not early_buyer_funders:
            return None

        # Direct funding collision: deployer and buyer funded by identical wallet
        shared = [f for f in early_buyer_funders if f == deployer_funder]
        if shared:
            log.warning(
                "adversarial.common_funder_detected",
                deployer_funder=deployer_funder,
                shared_count=len(shared),
            )
            return VetoReason.CEX_INSIDER_CABAL

        return None
