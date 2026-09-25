#!/usr/bin/env python3
"""
Botsensai Portfolio Worth Calculator
Queries on-chain token accounts, real-time DexScreener prices, entry costs,
and outputs the exact valuation of every token holding and total net worth.
"""

import json
import os
import sqlite3
import urllib.request
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

HOT_WALLET = "ChgCuBWDvGFwnW77kU523JcFXc2CryX3J4rEWDzWmtsS"
DB_PATH = Path("/home/ubuntu/Botsensai/data/botsensai.db")
if not DB_PATH.exists():
    DB_PATH = BASE_DIR / "data" / "botsensai.db"

RPC_URL = os.getenv("SOLANA_RPC_URL") or os.getenv("BOTSENSAI_SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"


def fetch_sol_price_usd() -> float:
    """Fetch current SOL price in USD from DexScreener or CoinGecko."""
    try:
        url = "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
            pairs = data.get("pairs") or []
            if pairs:
                for p in pairs:
                    if p.get("quoteToken", {}).get("symbol") in ("USDC", "USDT"):
                        return float(p.get("priceUsd") or 0.0)
                return float(pairs[0].get("priceUsd") or 0.0)
    except Exception:
        pass
    return 115.0  # Fallback estimate


def get_wallet_sol_balance() -> float:
    """Get native SOL balance of the hot wallet."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [HOT_WALLET]
    }).encode("utf-8")
    req = urllib.request.Request(
        RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Botsensai/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            res = json.loads(r.read().decode("utf-8"))
            lamports = res.get("result", {}).get("value", 0)
            return lamports / 1e9
    except Exception:
        return 0.0


def get_onchain_token_accounts() -> list[dict]:
    """Fetch all SPL token and Token-2022 accounts owned by hot wallet."""
    programs = [
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # Standard SPL
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
    ]
    tokens = []
    for pid in programs:
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                HOT_WALLET,
                {"programId": pid},
                {"encoding": "jsonParsed"}
            ]
        }).encode("utf-8")
        req = urllib.request.Request(
            RPC_URL,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "Botsensai/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                res = json.loads(r.read().decode("utf-8"))
                for acc in res.get("result", {}).get("value", []):
                    info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                    mint = info.get("mint")
                    amt_info = info.get("tokenAmount", {})
                    amount = float(amt_info.get("uiAmount") or 0.0)
                    decimals = int(amt_info.get("decimals") or 0)
                    if amount > 0:
                        tokens.append({
                            "mint": mint,
                            "amount": amount,
                            "decimals": decimals,
                            "program": "token-2022" if "Tokenz" in pid else "spl-token"
                        })
        except Exception as e:
            print(f"Error fetching token accounts for {pid}: {e}")
    return tokens


def fetch_token_market_data(mint: str) -> dict:
    """Fetch current market price in SOL & USD from DexScreener."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
            pairs = data.get("pairs") or []
            if pairs:
                p = pairs[0]
                price_usd = float(p.get("priceUsd") or 0.0)
                price_sol = float(p.get("priceNative") or 0.0)
                symbol = p.get("baseToken", {}).get("symbol", "")
                name = p.get("baseToken", {}).get("name", "")
                liq_usd = float(p.get("liquidity", {}).get("usd") or 0.0)
                return {
                    "price_usd": price_usd,
                    "price_sol": price_sol,
                    "symbol": symbol,
                    "name": name,
                    "liquidity_usd": liq_usd,
                }
    except Exception:
        pass
    return {
        "price_usd": 0.0,
        "price_sol": 0.0,
        "symbol": "",
        "name": "",
        "liquidity_usd": 0.0,
    }


def get_position_db_data(mint: str) -> dict:
    """Query live_positions DB for entry metadata."""
    if not DB_PATH.exists():
        return {}
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            row = cur.execute(
                "SELECT symbol, entry_price_sol, peak_price_sol, cost_sol, status FROM live_positions WHERE mint = ?",
                (mint,)
            ).fetchone()
            if row:
                return {
                    "symbol": row[0],
                    "entry_price_sol": float(row[1] or 0.0),
                    "peak_price_sol": float(row[2] or 0.0),
                    "cost_sol": float(row[3] or 0.0),
                    "status": row[4],
                }
    except Exception:
        pass
    return {}


def main():
    sol_price_usd = fetch_sol_price_usd()
    liquid_sol = get_wallet_sol_balance()
    liquid_usd = liquid_sol * sol_price_usd

    print("=" * 80)
    print(f"      BOTSENSAI HOT WALLET PORTFOLIO VALUATION REPORT")
    print(f"      Hot Wallet: {HOT_WALLET}")
    print(f"      SOL Price : ${sol_price_usd:.2f} USD | Liquid Cash: {liquid_sol:.6f} SOL (${liquid_usd:.2f})")
    print("=" * 80)

    token_accounts = get_onchain_token_accounts()
    print(f"Found {len(token_accounts)} non-zero on-chain token accounts.\n")

    total_tokens_sol = 0.0
    total_tokens_usd = 0.0

    print(f" {'#':<3} {'Symbol':<10} {'Amount Held':<16} {'Price (SOL)':<14} {'Price (USD)':<12} {'Value (SOL)':<14} {'Value (USD)':<12} {'Multiple'}")
    print(" " + "-" * 95)

    for idx, t in enumerate(token_accounts, 1):
        mint = t["mint"]
        amount = t["amount"]
        market = fetch_token_market_data(mint)
        db_data = get_position_db_data(mint)

        symbol = db_data.get("symbol") or market.get("symbol") or mint[:6]
        price_sol = market.get("price_sol", 0.0)
        price_usd = market.get("price_usd", 0.0)

        # Fallback if dexscreener priceNative is 0 but price_usd exists
        if price_sol == 0.0 and price_usd > 0.0 and sol_price_usd > 0.0:
            price_sol = price_usd / sol_price_usd

        # Fallback to DB entry price if newly launched pump token not yet indexed by dexscreener
        entry_sol = db_data.get("entry_price_sol", 0.0)
        if price_sol == 0.0 and entry_sol > 0.0:
            price_sol = entry_sol
            price_usd = entry_sol * sol_price_usd

        val_sol = amount * price_sol
        val_usd = amount * price_usd if price_usd > 0.0 else val_sol * sol_price_usd

        # Multiple vs entry
        if entry_sol > 0:
            mult = price_sol / entry_sol
            mult_str = f"{mult:.2f}x"
        else:
            mult_str = "N/A"

        total_tokens_sol += val_sol
        total_tokens_usd += val_usd

        amt_str = f"{amount:,.0f}" if amount >= 10 else f"{amount:.4f}"
        psol_str = f"{price_sol:.8f}" if price_sol > 0 else "<0.00000001"
        pusd_str = f"${price_usd:.6f}" if price_usd > 0 else "<$0.000001"
        vsol_str = f"{val_sol:.4f} SOL"
        vusd_str = f"${val_usd:.2f}"

        print(f" {idx:<3} {symbol:<10} {amt_str:<16} {psol_str:<14} {pusd_str:<12} {vsol_str:<14} {vusd_str:<12} {mult_str}")
        print(f"     Mint: {mint}")

    total_net_worth_sol = liquid_sol + total_tokens_sol
    total_net_worth_usd = liquid_usd + total_tokens_usd

    print(" " + "-" * 95)
    print(f" TOTAL TOKENS WORTH : {total_tokens_sol:.4f} SOL (${total_tokens_usd:.2f} USD)")
    print(f" LIQUID SOL CASH    : {liquid_sol:.4f} SOL (${liquid_usd:.2f} USD)")
    print(f" TOTAL NET WORTH    : {total_net_worth_sol:.4f} SOL (${total_net_worth_usd:.2f} USD)")
    print("=" * 80)


if __name__ == "__main__":
    main()
