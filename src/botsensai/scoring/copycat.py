"""Copycat clone and perceptual hash honeypot detector."""

from __future__ import annotations

from botsensai.config import Settings, get_settings
from botsensai.models import TokenRef, VetoReason
from botsensai.util.logging import get_logger

log = get_logger(__name__)

# Known high-profile tokens frequently targeted by automated clone bots
KNOWN_ORIGINAL_MINTS = {
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "POPCAT": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
    "FWOG": "A8C3xuqscfmyLrte3VmTqrAq8kgMASius9AFNANwpump",
}


class CopycatDetector:
    """Detects deceptive clone honeypots impersonating established memecoins."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def check_copycat(
        self,
        token: TokenRef,
        image_phash: str | None = None,
        rugged_phashes: set[str] | None = None,
    ) -> VetoReason | None:
        """Veto tokens impersonating famous tickers with fake mints or known rug imagery."""
        if not self.settings.scoring.veto_copycat:
            return None

        symbol = (token.symbol or "").upper()
        if symbol in KNOWN_ORIGINAL_MINTS:
            expected_mint = KNOWN_ORIGINAL_MINTS[symbol]
            if token.mint != expected_mint:
                log.warning("copycat.impersonation_detected", symbol=symbol, mint=token.mint)
                return VetoReason.COPYCAT_HONEYPOT

        if image_phash and rugged_phashes and image_phash in rugged_phashes:
            log.warning("copycat.rugged_phash_detected", phash=image_phash, mint=token.mint)
            return VetoReason.COPYCAT_HONEYPOT

        return None
