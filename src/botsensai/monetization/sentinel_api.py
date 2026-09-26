"""B2B Sentinel Security & Rent Reclaim API Router for Botsensai.

Provides endpoints for third-party bots, screeners, and web3 frontends:
1. /api/v1/sentinel/scan/{mint} - On-demand security and honeypot inspection.
2. /api/v1/reclaim/scan/{wallet} - Scans empty token accounts and trapped rent.
3. /api/v1/reclaim/build-tx - Builds non-custodial CloseAccount transaction with 90/10 fee split.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import transfer, TransferParams
from solders.transaction import VersionedTransaction

router = APIRouter(prefix="/api/v1", tags=["monetization"])

RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
TREASURY_WALLET = os.getenv("TREASURY_WALLET", "ChgCuBWDvGFwnW77kU523JcFXc2CryX3J4rEWDzWmtsS")
SPL_TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")


def _rpc(method: str, params: list[Any]) -> dict[str, Any]:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    req = urllib.request.Request(
        RPC_URL, data=payload.encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


# --- Pydantic Request / Response Models ---

class SentinelScanResponse(BaseModel):
    mint: str
    security_score: int = Field(..., description="0 to 100, where 100 is completely clean")
    risk_level: str = Field(..., description="LOW | MEDIUM | CRITICAL")
    is_honeypot: bool
    has_freeze_authority: bool
    has_mint_authority: bool
    has_permanent_delegate: bool
    token_program: str
    vetoes: list[str]
    audit_notes: list[str]


class ReclaimScanResponse(BaseModel):
    wallet: str
    total_token_accounts: int
    empty_accounts_count: int
    trapped_sol: float
    estimated_user_refund_sol: float
    estimated_protocol_fee_sol: float
    empty_accounts: list[dict[str, Any]]


class BuildReclaimTxRequest(BaseModel):
    wallet: str
    empty_accounts: list[str] = Field(..., description="List of empty ATA public keys to close")


class BuildReclaimTxResponse(BaseModel):
    serialized_transaction: str
    accounts_to_close: int
    user_refund_sol: float
    protocol_fee_sol: float


# --- Endpoints ---

@router.get("/sentinel/scan/{mint}", response_model=SentinelScanResponse)
async def scan_token_security(mint: str) -> SentinelScanResponse:
    """Perform real-time on-chain security audit of a Solana token mint."""
    try:
        mint_pubkey = Pubkey.from_string(mint)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Solana mint public key")

    res = _rpc("getAccountInfo", [str(mint_pubkey), {"encoding": "jsonParsed"}])
    value = res.get("result", {}).get("value")
    if not value:
        raise HTTPException(status_code=404, detail="Token mint not found on-chain")

    owner_prog = value.get("owner", "")
    parsed_info = value.get("data", {}).get("parsed", {}).get("info", {})

    freeze_auth = parsed_info.get("freezeAuthority")
    mint_auth = parsed_info.get("mintAuthority")
    extensions = parsed_info.get("extensions", [])

    has_perm_delegate = any(ext.get("extension") == "permanentDelegate" for ext in extensions)

    vetoes = []
    notes = []
    score = 100

    if freeze_auth is not None:
        vetoes.append("active_freeze_authority")
        notes.append(f"Freeze authority held by {freeze_auth}")
        score -= 40

    if has_perm_delegate:
        vetoes.append("weaponized_permanent_delegate")
        notes.append("Token-2022 permanent delegate extension present (high rug risk)")
        score -= 50

    if mint_auth is not None:
        vetoes.append("active_mint_authority")
        notes.append(f"Mint authority held by {mint_auth}")
        score -= 20

    is_honeypot = score <= 40 or has_perm_delegate
    risk = "CRITICAL" if is_honeypot else ("MEDIUM" if score <= 80 else "LOW")

    return SentinelScanResponse(
        mint=mint,
        security_score=max(0, score),
        risk_level=risk,
        is_honeypot=is_honeypot,
        has_freeze_authority=freeze_auth is not None,
        has_mint_authority=mint_auth is not None,
        has_permanent_delegate=has_perm_delegate,
        token_program="Token-2022" if owner_prog == str(TOKEN_2022_PROGRAM_ID) else "SPL-Token",
        vetoes=vetoes,
        audit_notes=notes,
    )


@router.get("/reclaim/scan/{wallet}", response_model=ReclaimScanResponse)
async def scan_reclaimable_rent(wallet: str) -> ReclaimScanResponse:
    """Scan all empty Associated Token Accounts for a wallet and compute reclaimable SOL."""
    try:
        wallet_pubkey = Pubkey.from_string(wallet)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Solana wallet public key")

    empty = []
    total = 0

    for prog in [SPL_TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID]:
        res = _rpc(
            "getTokenAccountsByOwner",
            [str(wallet_pubkey), {"programId": str(prog)}, {"encoding": "jsonParsed"}],
        )
        accs = res.get("result", {}).get("value", [])
        total += len(accs)
        for a in accs:
            amt = int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            lamports = a["account"]["lamports"]
            if amt == 0:
                empty.append({
                    "pubkey": a["pubkey"],
                    "mint": a["account"]["data"]["parsed"]["info"]["mint"],
                    "program": "Token-2022" if str(prog) == str(TOKEN_2022_PROGRAM_ID) else "SPL-Token",
                    "lamports": lamports,
                    "sol": round(lamports / 1e9, 6),
                })

    trapped_sol = sum(e["sol"] for e in empty)
    user_refund = round(trapped_sol * 0.90, 6)
    fee_sol = round(trapped_sol * 0.10, 6)

    return ReclaimScanResponse(
        wallet=wallet,
        total_token_accounts=total,
        empty_accounts_count=len(empty),
        trapped_sol=round(trapped_sol, 6),
        estimated_user_refund_sol=user_refund,
        estimated_protocol_fee_sol=fee_sol,
        empty_accounts=empty,
    )


@router.post("/reclaim/build-tx", response_model=BuildReclaimTxResponse)
async def build_reclaim_transaction(req: BuildReclaimTxRequest) -> BuildReclaimTxResponse:
    """Build an unsigned transaction for the user to close accounts and split 90% refund / 10% fee."""
    try:
        user_pubkey = Pubkey.from_string(req.wallet)
        treasury_pubkey = Pubkey.from_string(TREASURY_WALLET)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid public key")

    if not req.empty_accounts:
        raise HTTPException(status_code=400, detail="No accounts provided to close")

    instructions = []
    total_lamports = 0

    # Limit batch to 20 accounts per transaction to ensure fitting in standard MTU
    for acc_str in req.empty_accounts[:20]:
        try:
            acc_pubkey = Pubkey.from_string(acc_str)
        except Exception:
            raise HTTPException(status_code=400, detail=f"Invalid account public key: {acc_str}")
        # CloseAccount instruction (index 9)
        ix = Instruction(
            SPL_TOKEN_PROGRAM_ID,
            bytes([9]),
            [
                AccountMeta(acc_pubkey, is_signer=False, is_writable=True),
                AccountMeta(user_pubkey, is_signer=False, is_writable=True),
                AccountMeta(user_pubkey, is_signer=True, is_writable=True),
            ],
        )
        instructions.append(ix)
        total_lamports += 2039280  # Standard rent exemption

    # Add 10% protocol fee transfer to treasury
    fee_lamports = int(total_lamports * 0.10)
    if fee_lamports > 5000:
        fee_ix = transfer(
            TransferParams(
                from_pubkey=user_pubkey,
                to_pubkey=treasury_pubkey,
                lamports=fee_lamports,
            )
        )
        instructions.append(fee_ix)

    # Fetch recent blockhash
    bh_res = _rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
    recent_bh = Hash.from_string(bh_res["result"]["value"]["blockhash"])

    msg = MessageV0.try_compile(user_pubkey, instructions, [], recent_bh)
    # Return empty-signed VersionedTransaction for client-side signing
    sigs = [Signature.default() for _ in range(msg.header.num_required_signatures)]
    tx = VersionedTransaction.populate(msg, sigs)
    b64_tx = base64.b64encode(bytes(tx)).decode("utf-8")

    return BuildReclaimTxResponse(
        serialized_transaction=b64_tx,
        accounts_to_close=len(req.empty_accounts[:20]),
        user_refund_sol=round((total_lamports - fee_lamports) / 1e9, 6),
        protocol_fee_sol=round(fee_lamports / 1e9, 6),
    )
