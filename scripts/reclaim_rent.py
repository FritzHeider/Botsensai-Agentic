#!/usr/bin/env python3
"""Botsensai Rent Reclaimer & Empty Token Account Auditor.

Scans the hot wallet for empty SPL Token and Token-2022 accounts,
reports trapped rent deposits (~0.00204 SOL per account),
and cleanly submits on-chain CloseAccount instructions to recover the SOL.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DEFAULT_KEYPAIR = Path("/home/ubuntu/.config/solana/id.json")
if not DEFAULT_KEYPAIR.exists():
    DEFAULT_KEYPAIR = Path.home() / ".config" / "solana" / "id.json"

RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
SPL_TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")


def rpc_call(method: str, params: list) -> dict:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    req = urllib.request.Request(
        RPC_URL, data=payload.encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_token_accounts(wallet_pubkey: str, program_id: Pubkey) -> list[dict]:
    res = rpc_call(
        "getTokenAccountsByOwner",
        [wallet_pubkey, {"programId": str(program_id)}, {"encoding": "jsonParsed"}],
    )
    return res.get("result", {}).get("value", [])


def scan_and_reclaim(close_empty: bool = False, keypair_path: Path = DEFAULT_KEYPAIR):
    if not keypair_path.exists():
        print(f"Keypair not found at {keypair_path}")
        return

    with open(keypair_path, "r") as f:
        secret = json.load(f)
    keypair = Keypair.from_bytes(bytes(secret))
    wallet_pubkey = keypair.pubkey()

    print("=" * 75)
    print("   BOTSENSAI SOL RENT RECLAIM & TOKEN ACCOUNT AUDITOR")
    print(f"   Wallet: {wallet_pubkey}")
    print("=" * 75)

    programs = [
        ("SPL Token Program", SPL_TOKEN_PROGRAM_ID),
        ("Token-2022 Program", TOKEN_2022_PROGRAM_ID),
    ]

    total_accounts = 0
    empty_accounts = []
    active_accounts = []

    for name, prog_id in programs:
        accounts = get_token_accounts(str(wallet_pubkey), prog_id)
        total_accounts += len(accounts)
        print(f"\nScanning {name} ({len(accounts)} accounts found)...")

        for acc in accounts:
            pubkey_str = acc["pubkey"]
            info = acc["account"]["data"]["parsed"]["info"]
            token_amount_raw = int(info["tokenAmount"]["amount"])
            ui_amount = info["tokenAmount"]["uiAmount"]
            lamports = acc["account"]["lamports"]
            mint = info["mint"]

            if token_amount_raw == 0:
                empty_accounts.append((pubkey_str, prog_id, lamports, mint))
                print(f"  [EMPTY] {pubkey_str} | Mint: {mint} | Trapped Rent: {lamports / 1e9:.6f} SOL")
            else:
                active_accounts.append((pubkey_str, prog_id, lamports, mint, ui_amount))
                print(f"  [ACTIVE] {pubkey_str} | Mint: {mint} | Holding: {ui_amount} tokens ({lamports / 1e9:.6f} SOL rent)")

    trapped_sol = sum(item[2] for item in empty_accounts) / 1e9
    print("\n" + "-" * 75)
    print(f"Audit Summary:")
    print(f"  Total Token Accounts : {total_accounts}")
    print(f"  Active Accounts      : {len(active_accounts)}")
    print(f"  Empty Accounts       : {len(empty_accounts)}")
    print(f"  Reclaimable SOL Rent : {trapped_sol:.6f} SOL (${trapped_sol * 119.29:.2f} USD)")
    print("-" * 75)

    if not empty_accounts:
        print("\nAll empty accounts have already been closed and rent reclaimed!")
        print("No idle SOL is currently trapped in empty token accounts.")
        return

    if not close_empty:
        print("\nTo close empty accounts and reclaim SOL directly to your wallet, run with --close")
        return

    print(f"\nClosing {len(empty_accounts)} empty accounts...")
    instructions = []
    for pubkey_str, prog_id, lamports, mint in empty_accounts:
        acc_pubkey = Pubkey.from_string(pubkey_str)
        # SPL CloseAccount Instruction layout: instruction_index = 9
        # Accounts: [account_to_close (writable), destination (writable), owner (signer)]
        ix = Instruction(
            prog_id,
            bytes([9]),
            [
                AccountMeta(acc_pubkey, is_signer=False, is_writable=True),
                AccountMeta(wallet_pubkey, is_signer=False, is_writable=True),
                AccountMeta(wallet_pubkey, is_signer=True, is_writable=True),
            ],
        )
        instructions.append(ix)

    # Get recent blockhash
    blockhash_res = rpc_call("getLatestBlockhash", [{"commitment": "confirmed"}])
    blockhash_str = blockhash_res["result"]["value"]["blockhash"]
    recent_blockhash = Hash.from_string(blockhash_str)

    msg = MessageV0.try_compile(wallet_pubkey, instructions, [], recent_blockhash)
    tx = VersionedTransaction(msg, [keypair])
    tx_bytes = bytes(tx)
    b64_tx = base64.b64encode(tx_bytes).decode("utf-8")

    send_res = rpc_call(
        "sendTransaction",
        [b64_tx, {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"}],
    )
    if "error" in send_res:
        print(f"Error sending close transaction: {send_res['error']}")
    else:
        sig = send_res.get("result")
        print(f"\nSuccessfully broadcasted CloseAccount transaction!")
        print(f"Transaction Signature: https://solscan.io/tx/{sig}")
        print(f"Reclaimed {trapped_sol:.6f} SOL to {wallet_pubkey}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit and reclaim SOL rent from empty token accounts.")
    parser.add_argument("--close", action="store_true", help="Close empty accounts and reclaim SOL.")
    parser.add_argument("--keypair", type=Path, default=DEFAULT_KEYPAIR, help="Path to Solana keypair.")
    args = parser.parse_args()

    scan_and_reclaim(close_empty=args.close, keypair_path=args.keypair)
