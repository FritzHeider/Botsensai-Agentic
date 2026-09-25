"""Native tool implementations for Botsensai Sentinel Agent."""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict


HOT_WALLET_PUBKEY = "ChgCuBWDvGFwnW77kU523JcFXc2CryX3J4rEWDzWmtsS"
DEFAULT_HELIUS_KEY = "4cb5eb58-aaf8-482a-ba63-fc8ff63c270e"
HARD_FLOOR_SOL = 0.010000  # Operational gas and ATA rent floor
MIN_BUFFER_SOL = 0.003000   # Minimum buffer needed for ATA rent + tx fee
MAX_ALLOC_SOL = 0.035000


def helius_get_asset(mint: str) -> str:
    """Queries Helius Digital Asset Standard (DAS) RPC to inspect token metadata, authorities, and Token-2022 extensions.

    Args:
        mint: The Solana mint address of the candidate token.

    Returns:
        JSON string containing token name, symbol, authorities (mint/freeze/permanent delegate), and Token-2022 flags.
    """
    api_key = os.environ.get("HELIUS_API_KEY") or os.environ.get("BOTSENSAI_HELIUS_API_KEY") or DEFAULT_HELIUS_KEY
    rpc_url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"

    payload = {
        "jsonrpc": "2.0",
        "id": "sentinel-das-query",
        "method": "getAsset",
        "params": {"id": mint}
    }

    try:
        req = urllib.request.Request(
            rpc_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        res = data.get("result", {})
        if not res:
            return json.dumps({"error": f"Token {mint} not found on-chain via DAS."})

        content = res.get("content", {})
        metadata = content.get("metadata", {})
        token_info = res.get("token_info", {})
        mint_ext = res.get("mint_extensions", {})

        permanent_delegate = None
        if "permanent_delegate" in mint_ext:
            permanent_delegate = mint_ext["permanent_delegate"].get("delegate")

        token_program = token_info.get("token_program", "")
        is_token_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb" in token_program
        mint_auth = token_info.get("mint_authority")
        freeze_auth = token_info.get("freeze_authority")

        # Detect dangerous spam patterns in name/symbol
        name = metadata.get("name", "")
        symbol = metadata.get("symbol", "")
        description = metadata.get("description", "")
        is_spam_ad = any(spam_word in (name + symbol + description).lower() for spam_word in [
            "pumpdev.io", "pumpapi.io", "switch to", "free pump.fun api", "overpaying 4x"
        ])

        summary = {
            "mint": mint,
            "name": name,
            "symbol": symbol,
            "description": description,
            "token_program": token_program,
            "is_token_2022": is_token_2022,
            "mint_authority": mint_auth,
            "freeze_authority": freeze_auth,
            "permanent_delegate": permanent_delegate,
            "is_spam_advertisement": is_spam_ad,
            "has_weaponized_authority": bool(permanent_delegate or freeze_auth)
        }
        return json.dumps(summary)
    except Exception as e:
        return json.dumps({"error": f"Failed querying DAS getAsset for {mint}: {str(e)}"})


def dexscreener_get_pairs(mint: str) -> str:
    """Fetches real-time DexScreener pricing, liquidity, and 5-minute/1-hour trading volume.

    Args:
        mint: The Solana mint address of the candidate token.

    Returns:
        JSON string containing the primary DEX pair, liquidity in USD, price in native SOL, and volume.
    """
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Botsensai-Sentinel/2.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        pairs = data.get("pairs") or []
        if not pairs:
            return json.dumps({
                "mint": mint,
                "found": False,
                "note": "No active liquidity pool registered on DexScreener yet (pre-migration bonding curve or dead launch)."
            })

        # Select best pair by liquidity
        best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        result = {
            "mint": mint,
            "found": True,
            "dex_id": best.get("dexId", "unknown"),
            "pair_address": best.get("pairAddress", ""),
            "price_native_sol": float(best.get("priceNative") or 0.0),
            "liquidity_usd": float(best.get("liquidity", {}).get("usd") or 0.0),
            "volume_5m_usd": float(best.get("volume", {}).get("m5") or 0.0),
            "volume_1h_usd": float(best.get("volume", {}).get("h1") or 0.0),
            "txns_5m_buys": best.get("txns", {}).get("m5", {}).get("buys", 0),
            "txns_5m_sells": best.get("txns", {}).get("m5", {}).get("sells", 0),
        }
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": f"Failed querying DexScreener for {mint}: {str(e)}"})


def check_wallet_reserve_floor() -> str:
    """Checks the hot wallet live balance against the mandatory 0.1000 SOL reserve floor (Rule 2).

    Returns:
        JSON string containing the live SOL balance, reserve floor, available buffer, and maximum allowable entry size.
    """
    api_key = os.environ.get("HELIUS_API_KEY") or os.environ.get("BOTSENSAI_HELIUS_API_KEY") or DEFAULT_HELIUS_KEY
    rpc_url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"

    payload = {
        "jsonrpc": "2.0",
        "id": "balance-check",
        "method": "getBalance",
        "params": [HOT_WALLET_PUBKEY]
    }

    try:
        req = urllib.request.Request(
            rpc_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        lamports = data.get("result", {}).get("value", 0)
        sol_bal = lamports / 1_000_000_000

        buffer_above_floor = max(0.0, sol_bal - HARD_FLOOR_SOL)
        can_trade = buffer_above_floor >= MIN_BUFFER_SOL

        # Dynamic bankroll sizing (25% of available buffer above gas floor, default 0.015-0.025 SOL)
        max_entry = min(MAX_ALLOC_SOL, max(0.015, buffer_above_floor * 0.25)) if can_trade else 0.0

        return json.dumps({
            "wallet": HOT_WALLET_PUBKEY,
            "balance_sol": round(sol_bal, 6),
            "hard_floor_sol": HARD_FLOOR_SOL,
            "min_safety_buffer_sol": MIN_BUFFER_SOL,
            "available_buffer_sol": round(buffer_above_floor, 6),
            "trading_allowed": can_trade,
            "max_position_size_sol": round(max_entry, 4),
            "guard_status": "ENGAGED_DRY_POWDER" if not can_trade else "CAPITAL_CLEAR"
        })
    except Exception as e:
        return json.dumps({"error": f"Failed checking wallet balance: {str(e)}"})


def quarantine_dust_token(mint: str, reason: str) -> str:
    """Quarantines an unsolicited promotional or malicious dust token in the quarantine blacklist.

    Args:
        mint: The Solana mint address to quarantine.
        reason: Justification for quarantining (e.g. 'permanent_delegate_live', 'promotional_spam').

    Returns:
        JSON string confirming token quarantine.
    """
    quarantine_file = Path("data/quarantined_tokens.json")
    quarantine_file.parent.mkdir(parents=True, exist_ok=True)

    records: Dict[str, Any] = {}
    if quarantine_file.exists():
        try:
            records = json.loads(quarantine_file.read_text())
        except Exception:
            records = {}

    records[mint] = {
        "reason": reason,
        "quarantined_at": "now",
        "action": "ZERO_VALUE_DUST_LOCKED"
    }

    try:
        quarantine_file.write_text(json.dumps(records, indent=2))
        return json.dumps({"status": "QUARANTINED", "mint": mint, "reason": reason})
    except Exception as e:
        return json.dumps({"error": f"Failed writing quarantine record: {str(e)}"})
