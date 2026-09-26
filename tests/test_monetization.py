"""Automated Test Suite for Botsensai Monetization Funnels.

Covers:
1. GET /api/v1/sentinel/scan/{mint} (B2B Security audit, clean vs malicious token extensions)
2. GET /api/v1/reclaim/scan/{wallet} (Empty ATA detection, trapped SOL and 90/10 fee calculation)
3. POST /api/v1/reclaim/build-tx (VersionedTransaction construction with 90% user / 10% treasury fee split)
4. TelegramAlphaBot user tier data model (Free, Pro, Whale)
5. Referral code parsing and 20% revenue share attribution
6. Signal formatting (>=0.90 ultra-conviction gate, paper-trade leak prevention)
7. 1-click copy-trading callback payloads and 0.010 SOL gas reserve floor enforcement
"""

import base64
from typing import Any
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from solders.hash import Hash
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from botsensai.api import create_headless_api_app
from botsensai.config import Settings
from botsensai.monetization import (
    BuildReclaimTxRequest,
    BuildReclaimTxResponse,
    ReclaimScanResponse,
    SentinelScanResponse,
    TelegramAlphaBot,
    UserProfile,
    UserTier,
    build_signal_keyboard,
    format_copy_trade_callback,
    format_signal_message,
    monetization_router,
    parse_copy_trade_callback,
    parse_referral_code,
)
import botsensai.monetization.sentinel_api as sentinel_module


# --- Fixtures ---

@pytest.fixture
def api_client():
    """FastAPI TestClient with monetization router mounted."""
    app = FastAPI(title="Botsensai Monetization Test")
    app.include_router(monetization_router)
    return TestClient(app)


@pytest.fixture
def headless_client(tmp_path):
    """Full headless API app TestClient."""
    settings = Settings(db_path=str(tmp_path / "test_monetization.db"))
    app = create_headless_api_app(settings)
    return TestClient(app)


@pytest.fixture
def sample_pubkeys():
    """Generate realistic Solana public keys for testing."""
    return {
        "clean_mint": str(Pubkey.new_unique()),
        "malicious_mint": str(Pubkey.new_unique()),
        "user_wallet": str(Pubkey.new_unique()),
        "treasury": sentinel_module.TREASURY_WALLET,
        "empty_ata_1": str(Pubkey.new_unique()),
        "empty_ata_2": str(Pubkey.new_unique()),
        "empty_ata_3": str(Pubkey.new_unique()),
        "funded_ata": str(Pubkey.new_unique()),
    }


# ==============================================================================
# 1. B2B Sentinel Security API Tests (GET /api/v1/sentinel/scan/{mint})
# ==============================================================================

class TestSentinelSecurityAPI:
    """Test suite for on-chain token security auditing."""

    def test_scan_clean_spl_token(self, api_client, monkeypatch, sample_pubkeys):
        """Clean standard SPL-Token with renounced authorities."""
        mint = sample_pubkeys["clean_mint"]

        def mock_rpc(method, params):
            assert method == "getAccountInfo"
            assert params[0] == mint
            return {
                "result": {
                    "value": {
                        "owner": str(sentinel_module.SPL_TOKEN_PROGRAM_ID),
                        "data": {
                            "parsed": {
                                "info": {
                                    "freezeAuthority": None,
                                    "mintAuthority": None,
                                    "extensions": [],
                                }
                            }
                        },
                    }
                }
            }

        monkeypatch.setattr(sentinel_module, "_rpc", mock_rpc)

        resp = api_client.get(f"/api/v1/sentinel/scan/{mint}")
        assert resp.status_code == 200
        data = resp.json()

        assert data["mint"] == mint
        assert data["security_score"] == 100
        assert data["risk_level"] == "LOW"
        assert data["is_honeypot"] is False
        assert data["has_freeze_authority"] is False
        assert data["has_mint_authority"] is False
        assert data["has_permanent_delegate"] is False
        assert data["token_program"] == "SPL-Token"
        assert data["vetoes"] == []

    def test_scan_malicious_token_2022_permanent_delegate(
        self, api_client, monkeypatch, sample_pubkeys
    ):
        """Token-2022 mint containing weaponized permanentDelegate extension."""
        mint = sample_pubkeys["malicious_mint"]

        def mock_rpc(method, params):
            return {
                "result": {
                    "value": {
                        "owner": str(sentinel_module.TOKEN_2022_PROGRAM_ID),
                        "data": {
                            "parsed": {
                                "info": {
                                    "freezeAuthority": None,
                                    "mintAuthority": None,
                                    "extensions": [
                                        {
                                            "extension": "permanentDelegate",
                                            "state": {"delegate": str(Pubkey.new_unique())},
                                        }
                                    ],
                                }
                            }
                        },
                    }
                }
            }

        monkeypatch.setattr(sentinel_module, "_rpc", mock_rpc)

        resp = api_client.get(f"/api/v1/sentinel/scan/{mint}")
        assert resp.status_code == 200
        data = resp.json()

        assert data["mint"] == mint
        assert data["security_score"] == 50  # 100 - 50 penalty
        assert data["risk_level"] == "CRITICAL"
        assert data["is_honeypot"] is True
        assert data["has_permanent_delegate"] is True
        assert data["token_program"] == "Token-2022"
        assert "weaponized_permanent_delegate" in data["vetoes"]

    def test_scan_malicious_active_freeze_and_mint_authority(
        self, api_client, monkeypatch, sample_pubkeys
    ):
        """Token with active freeze authority and active mint authority (honeypot)."""
        mint = sample_pubkeys["malicious_mint"]
        dev_wallet = str(Pubkey.new_unique())

        def mock_rpc(method, params):
            return {
                "result": {
                    "value": {
                        "owner": str(sentinel_module.SPL_TOKEN_PROGRAM_ID),
                        "data": {
                            "parsed": {
                                "info": {
                                    "freezeAuthority": dev_wallet,
                                    "mintAuthority": dev_wallet,
                                    "extensions": [],
                                }
                            }
                        },
                    }
                }
            }

        monkeypatch.setattr(sentinel_module, "_rpc", mock_rpc)

        resp = api_client.get(f"/api/v1/sentinel/scan/{mint}")
        assert resp.status_code == 200
        data = resp.json()

        # 100 - 40 (freeze) - 20 (mint) = 40 (honeypot <= 40)
        assert data["security_score"] == 40
        assert data["risk_level"] == "CRITICAL"
        assert data["is_honeypot"] is True
        assert data["has_freeze_authority"] is True
        assert data["has_mint_authority"] is True
        assert "active_freeze_authority" in data["vetoes"]
        assert "active_mint_authority" in data["vetoes"]

    def test_scan_medium_risk_mint_authority_only(
        self, api_client, monkeypatch, sample_pubkeys
    ):
        """Token with active mint authority but renounced freeze authority."""
        mint = sample_pubkeys["clean_mint"]
        dev_wallet = str(Pubkey.new_unique())

        def mock_rpc(method, params):
            return {
                "result": {
                    "value": {
                        "owner": str(sentinel_module.SPL_TOKEN_PROGRAM_ID),
                        "data": {
                            "parsed": {
                                "info": {
                                    "freezeAuthority": None,
                                    "mintAuthority": dev_wallet,
                                    "extensions": [],
                                }
                            }
                        },
                    }
                }
            }

        monkeypatch.setattr(sentinel_module, "_rpc", mock_rpc)

        resp = api_client.get(f"/api/v1/sentinel/scan/{mint}")
        assert resp.status_code == 200
        data = resp.json()

        assert data["security_score"] == 80  # 100 - 20
        assert data["risk_level"] == "MEDIUM"
        assert data["is_honeypot"] is False
        assert data["has_mint_authority"] is True
        assert data["has_freeze_authority"] is False
        assert data["vetoes"] == ["active_mint_authority"]

    def test_scan_invalid_mint_pubkey(self, api_client):
        """Invalid base58 pubkey string returns 400."""
        resp = api_client.get("/api/v1/sentinel/scan/not-a-valid-solana-pubkey")
        assert resp.status_code == 400
        assert "Invalid Solana mint public key" in resp.json()["detail"]

    def test_scan_mint_not_found(self, api_client, monkeypatch, sample_pubkeys):
        """Mint address not found on-chain returns 404."""
        mint = sample_pubkeys["clean_mint"]

        def mock_rpc(method, params):
            return {"result": {"value": None}}

        monkeypatch.setattr(sentinel_module, "_rpc", mock_rpc)

        resp = api_client.get(f"/api/v1/sentinel/scan/{mint}")
        assert resp.status_code == 404
        assert "Token mint not found on-chain" in resp.json()["detail"]


# ==============================================================================
# 2. Solana Rent Reclaim Scan (GET /api/v1/reclaim/scan/{wallet})
# ==============================================================================

class TestReclaimScanAPI:
    """Test suite for empty ATA scanning and trapped SOL calculation."""

    def test_reclaim_scan_identifies_empty_atas_and_computes_90_10_split(
        self, api_client, monkeypatch, sample_pubkeys
    ):
        """Identifies empty token accounts across SPL and Token-2022 programs."""
        wallet = sample_pubkeys["user_wallet"]
        ata1 = sample_pubkeys["empty_ata_1"]
        ata2 = sample_pubkeys["empty_ata_2"]
        ata3 = sample_pubkeys["empty_ata_3"]
        funded_ata = sample_pubkeys["funded_ata"]

        # Standard rent exemption per ATA is 2,039,280 lamports (~0.002039 SOL)
        rent_lamports = 2039280

        def mock_rpc(method, params):
            assert method == "getTokenAccountsByOwner"
            prog_id = params[1]["programId"]
            if prog_id == str(sentinel_module.SPL_TOKEN_PROGRAM_ID):
                return {
                    "result": {
                        "value": [
                            {
                                "pubkey": ata1,
                                "account": {
                                    "lamports": rent_lamports,
                                    "data": {
                                        "parsed": {
                                            "info": {
                                                "mint": sample_pubkeys["clean_mint"],
                                                "tokenAmount": {"amount": "0"},
                                            }
                                        }
                                    },
                                },
                            },
                            {
                                "pubkey": funded_ata,
                                "account": {
                                    "lamports": rent_lamports,
                                    "data": {
                                        "parsed": {
                                            "info": {
                                                "mint": sample_pubkeys["clean_mint"],
                                                "tokenAmount": {"amount": "1000000"},  # Funded!
                                            }
                                        }
                                    },
                                },
                            },
                            {
                                "pubkey": ata2,
                                "account": {
                                    "lamports": rent_lamports,
                                    "data": {
                                        "parsed": {
                                            "info": {
                                                "mint": sample_pubkeys["malicious_mint"],
                                                "tokenAmount": {"amount": "0"},
                                            }
                                        }
                                    },
                                },
                            },
                        ]
                    }
                }
            elif prog_id == str(sentinel_module.TOKEN_2022_PROGRAM_ID):
                return {
                    "result": {
                        "value": [
                            {
                                "pubkey": ata3,
                                "account": {
                                    "lamports": rent_lamports,
                                    "data": {
                                        "parsed": {
                                            "info": {
                                                "mint": str(Pubkey.new_unique()),
                                                "tokenAmount": {"amount": "0"},
                                            }
                                        }
                                    },
                                },
                            }
                        ]
                    }
                }
            return {"result": {"value": []}}

        monkeypatch.setattr(sentinel_module, "_rpc", mock_rpc)

        resp = api_client.get(f"/api/v1/reclaim/scan/{wallet}")
        assert resp.status_code == 200
        data = resp.json()

        assert data["wallet"] == wallet
        assert data["total_token_accounts"] == 4
        assert data["empty_accounts_count"] == 3

        # 3 accounts * round(2039280 / 1e9, 6) = 3 * 0.002039 = 0.006117 SOL trapped
        expected_trapped = round(3 * round(rent_lamports / 1e9, 6), 6)
        expected_user_refund = round(expected_trapped * 0.90, 6)
        expected_protocol_fee = round(expected_trapped * 0.10, 6)

        assert data["trapped_sol"] == expected_trapped
        assert data["estimated_user_refund_sol"] == expected_user_refund
        assert data["estimated_protocol_fee_sol"] == expected_protocol_fee
        assert round(data["estimated_user_refund_sol"] + data["estimated_protocol_fee_sol"], 6) == expected_trapped

        # Verify programs attributed correctly
        empty_pubkeys = [a["pubkey"] for a in data["empty_accounts"]]
        assert ata1 in empty_pubkeys
        assert ata2 in empty_pubkeys
        assert ata3 in empty_pubkeys
        assert funded_ata not in empty_pubkeys

    def test_reclaim_scan_zero_empty_accounts(
        self, api_client, monkeypatch, sample_pubkeys
    ):
        """Wallet with only funded token accounts yields 0 reclaimable SOL."""
        wallet = sample_pubkeys["user_wallet"]

        def mock_rpc(method, params):
            return {
                "result": {
                    "value": [
                        {
                            "pubkey": sample_pubkeys["funded_ata"],
                            "account": {
                                "lamports": 2039280,
                                "data": {
                                    "parsed": {
                                        "info": {
                                            "mint": sample_pubkeys["clean_mint"],
                                            "tokenAmount": {"amount": "50000"},
                                        }
                                    }
                                },
                            },
                        }
                    ]
                }
            }

        monkeypatch.setattr(sentinel_module, "_rpc", mock_rpc)

        resp = api_client.get(f"/api/v1/reclaim/scan/{wallet}")
        assert resp.status_code == 200
        data = resp.json()

        assert data["empty_accounts_count"] == 0
        assert data["trapped_sol"] == 0.0
        assert data["estimated_user_refund_sol"] == 0.0
        assert data["estimated_protocol_fee_sol"] == 0.0
        assert data["empty_accounts"] == []

    def test_reclaim_scan_invalid_wallet_pubkey(self, api_client):
        """Invalid base58 wallet pubkey returns 400."""
        resp = api_client.get("/api/v1/reclaim/scan/invalid_solana_address")
        assert resp.status_code == 400
        assert "Invalid Solana wallet public key" in resp.json()["detail"]


# ==============================================================================
# 3. Transaction Builder & Fee Routing (POST /api/v1/reclaim/build-tx)
# ==============================================================================

class TestReclaimBuildTxAPI:
    """Test suite for atomic CloseAccount + 90/10 treasury transfer transaction builder."""

    def test_build_reclaim_tx_instruction_structure_and_fee_split(
        self, api_client, monkeypatch, sample_pubkeys
    ):
        """Verify transaction construction: N CloseAccount ix + 1 Fee Transfer ix to Treasury."""
        wallet = sample_pubkeys["user_wallet"]
        empty_atas = [
            sample_pubkeys["empty_ata_1"],
            sample_pubkeys["empty_ata_2"],
            sample_pubkeys["empty_ata_3"],
        ]
        recent_blockhash_str = "EkSnNWid2cvwEVnVx9aFiSiqdaHrPpYTJxcnjP2bipn"

        def mock_rpc(method, params):
            if method == "getLatestBlockhash":
                return {
                    "result": {
                        "value": {
                            "blockhash": recent_blockhash_str,
                            "lastValidBlockHeight": 123456789,
                        }
                    }
                }
            raise ValueError(f"Unexpected RPC call: {method}")

        monkeypatch.setattr(sentinel_module, "_rpc", mock_rpc)

        req_body = {
            "wallet": wallet,
            "empty_accounts": empty_atas,
        }

        resp = api_client.post("/api/v1/reclaim/build-tx", json=req_body)
        assert resp.status_code == 200
        data = resp.json()

        assert data["accounts_to_close"] == 3

        # Rent calculations: 3 accounts * 2,039,280 = 6,117,840 lamports
        total_lamports = 3 * 2039280
        expected_fee_lamports = int(total_lamports * 0.10)
        expected_user_lamports = total_lamports - expected_fee_lamports

        expected_user_sol = round(expected_user_lamports / 1e9, 6)
        expected_fee_sol = round(expected_fee_lamports / 1e9, 6)

        assert data["user_refund_sol"] == expected_user_sol
        assert data["protocol_fee_sol"] == expected_fee_sol
        assert round(data["user_refund_sol"] + data["protocol_fee_sol"], 6) == round(total_lamports / 1e9, 6)

        # Decode and inspect deserialized VersionedTransaction
        tx_bytes = base64.b64decode(data["serialized_transaction"])
        tx = VersionedTransaction.from_bytes(tx_bytes)

        # Verify transaction header and accounts
        account_keys = tx.message.account_keys
        account_keys_str = [str(k) for k in account_keys]

        # Signer must be the user wallet
        assert str(account_keys[0]) == wallet

        # Treasury wallet must be in account keys for the fee transfer
        assert sentinel_module.TREASURY_WALLET in account_keys_str

        # All empty ATAs must be in account keys
        for ata in empty_atas:
            assert ata in account_keys_str

        # Total instructions: 3 CloseAccount instructions + 1 SystemProgram Transfer instruction
        instructions = tx.message.instructions
        assert len(instructions) == 4

        # Verify CloseAccount instructions (indices 0, 1, 2)
        spl_prog_index = account_keys_str.index(str(sentinel_module.SPL_TOKEN_PROGRAM_ID))
        for i in range(3):
            ix = instructions[i]
            assert ix.program_id_index == spl_prog_index
            assert bytes(ix.data) == bytes([9])  # SPL Token CloseAccount instruction discriminator

        # Verify 10% Fee Transfer instruction (index 3)
        fee_ix = instructions[3]
        system_prog_index = account_keys_str.index("11111111111111111111111111111111")
        assert fee_ix.program_id_index == system_prog_index

    def test_build_reclaim_tx_caps_at_twenty_accounts(
        self, api_client, monkeypatch, sample_pubkeys
    ):
        """Batch size is capped at 20 accounts per transaction to prevent MTU overflow."""
        wallet = sample_pubkeys["user_wallet"]
        thirty_atas = [str(Pubkey.new_unique()) for _ in range(30)]

        def mock_rpc(method, params):
            return {
                "result": {
                    "value": {
                        "blockhash": "EkSnNWid2cvwEVnVx9aFiSiqdaHrPpYTJxcnjP2bipn",
                        "lastValidBlockHeight": 123456789,
                    }
                }
            }

        monkeypatch.setattr(sentinel_module, "_rpc", mock_rpc)

        resp = api_client.post(
            "/api/v1/reclaim/build-tx",
            json={"wallet": wallet, "empty_accounts": thirty_atas},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["accounts_to_close"] == 20  # Capped at 20

    def test_build_reclaim_tx_empty_accounts_validation(self, api_client, sample_pubkeys):
        """Empty accounts list returns 400 Bad Request."""
        resp = api_client.post(
            "/api/v1/reclaim/build-tx",
            json={"wallet": sample_pubkeys["user_wallet"], "empty_accounts": []},
        )
        assert resp.status_code == 400
        assert "No accounts provided to close" in resp.json()["detail"]

    def test_build_reclaim_tx_invalid_wallet(self, api_client):
        """Invalid user wallet returns 400 Bad Request."""
        resp = api_client.post(
            "/api/v1/reclaim/build-tx",
            json={"wallet": "invalid_wallet", "empty_accounts": [str(Pubkey.new_unique())]},
        )
        assert resp.status_code == 400
        assert "Invalid public key" in resp.json()["detail"]

    def test_build_reclaim_tx_invalid_account_pubkey(self, api_client, sample_pubkeys):
        """Invalid ATA pubkey in list returns 400 Bad Request."""
        resp = api_client.post(
            "/api/v1/reclaim/build-tx",
            json={
                "wallet": sample_pubkeys["user_wallet"],
                "empty_accounts": ["not_a_valid_pubkey"],
            },
        )
        assert resp.status_code == 400
        assert "Invalid account public key" in resp.json()["detail"]


# ==============================================================================
# 4. TelegramAlphaBot User Tier Data Model & Subscriptions
# ==============================================================================

class TestTelegramBotUserTiers:
    """Test suite for Free, Pro, and Whale user tiers."""

    def test_default_user_registration_is_free_tier(self):
        bot = TelegramAlphaBot()
        user = bot.register_user(chat_id="111222", username="DeFiExplorer")

        assert user.chat_id == "111222"
        assert user.username == "DeFiExplorer"
        assert user.tier == UserTier.FREE
        assert user.fee_bps == 100  # 1.00%
        assert user.max_trade_sol == 1.0
        assert user.daily_scans == 10
        assert user.referral_code == "REF_111222"
        assert user.referred_by is None

    def test_tier_upgrades_and_privileges(self):
        bot = TelegramAlphaBot()
        user = bot.register_user(chat_id="333444", username="TopTrader")

        # Upgrade to Pro
        bot.set_user_tier("333444", UserTier.PRO)
        assert user.tier == UserTier.PRO
        assert user.fee_bps == 50  # 0.50%
        assert user.max_trade_sol == 10.0
        assert user.daily_scans == 500

        # Upgrade to Whale
        bot.set_user_tier("333444", UserTier.WHALE)
        assert user.tier == UserTier.WHALE
        assert user.fee_bps == 20  # 0.20%
        assert user.max_trade_sol == 100.0
        assert user.daily_scans == -1  # Unlimited


# ==============================================================================
# 5. Referral Code Parsing & 20% Revenue Share Attribution
# ==============================================================================

class TestReferralSystemAndRevenueAttribution:
    """Test suite for referral link parsing and 20% revenue share attribution."""

    @pytest.mark.parametrize(
        "payload,expected",
        [
            ("/start ref_ALICE123", "ALICE123"),
            ("/start ALICE123", "ALICE123"),
            ("/ref BOB789", "BOB789"),
            ("/referral WHALE01", "WHALE01"),
            ("ref_CHARLIE55", "CHARLIE55"),
            ("DIRECT_CODE", "DIRECT_CODE"),
            ("/start", None),
            ("/ref", None),
            ("", None),
            (None, None),
        ],
    )
    def test_parse_referral_code_formats(self, payload, expected):
        assert parse_referral_code(payload) == expected

    def test_referral_registration_and_binding(self):
        bot = TelegramAlphaBot()

        # Alice registers first
        alice = bot.register_user(chat_id="1001", username="Alice")
        assert alice.referral_code == "REF_1001"
        assert alice.referral_count == 0

        # Bob registers using Alice's referral code
        bob = bot.register_user(chat_id="1002", username="Bob", referral_code="/start ref_REF_1001")
        assert bob.referred_by == "REF_1001"
        assert alice.referral_count == 1

        # Self-referral prevention: Alice cannot refer herself
        alice_dup = bot.register_user(chat_id="1003", username="AliceAlt", referral_code="REF_1003")
        assert alice_dup.referred_by is None

    def test_revenue_share_attribution_20_percent_split(self):
        """20% of trading fees generated by referred users is credited to referrer."""
        bot = TelegramAlphaBot()

        # Register referrer (Alice) and referee (Bob)
        alice = bot.register_user(chat_id="1001", username="Alice")
        bob = bot.register_user(chat_id="1002", username="Bob", referral_code="REF_1001")

        trade_amount_sol = 5.0
        trade_fee_sol = 0.050  # 1% trading fee

        # Attribute revenue share on Bob's trade
        result = bot.attribute_revenue_share(
            trader_chat_id="1002",
            trade_amount_sol=trade_amount_sol,
            fee_sol=trade_fee_sol,
            rev_share_pct=0.20,
        )

        assert result["attributed"] is True
        assert result["trader_chat_id"] == "1002"
        assert result["referrer_chat_id"] == "1001"
        assert result["referrer_code"] == "REF_1001"
        assert result["rev_share_pct"] == 0.20

        # 20% of 0.050 SOL = 0.010 SOL to Alice
        assert result["referral_payout_sol"] == 0.010
        # 80% to Treasury = 0.040 SOL
        assert result["treasury_payout_sol"] == 0.040

        # Referrer earnings balance updated
        assert alice.referral_earnings_sol == 0.010

        # Second trade accrues cumulatively
        bot.attribute_revenue_share(
            trader_chat_id="1002",
            trade_amount_sol=10.0,
            fee_sol=0.100,
            rev_share_pct=0.20,
        )
        assert alice.referral_earnings_sol == 0.030  # 0.010 + 0.020

    def test_unreferred_user_trade_routes_100_percent_to_treasury(self):
        """Traders without referrers route 100% of fees to Treasury."""
        bot = TelegramAlphaBot()
        charlie = bot.register_user(chat_id="1003", username="Charlie")

        result = bot.attribute_revenue_share(
            trader_chat_id="1003",
            trade_amount_sol=2.0,
            fee_sol=0.020,
        )

        assert result["attributed"] is False
        assert result["referrer_chat_id"] is None
        assert result["referral_payout_sol"] == 0.0
        assert result["treasury_payout_sol"] == 0.020


# ==============================================================================
# 6. Ultra-Conviction Signal Formatting & Paper Trade Leak Prevention
# ==============================================================================

class TestSignalFormattingAndBotsensaiRules:
    """Test suite for ultra-conviction signal gating and anti-leak rules."""

    def test_ultra_conviction_signal_formatting(self):
        """Signals with score >= 0.90 format institutional copy-trading announcement."""
        signal = {
            "symbol": "SOLAR",
            "mint": "So11111111111111111111111111111111111111112",
            "score": 0.942,
            "regime": "high_volatility",
            "stage_exit": "2.0x (+100%)",
            "moonbag_pct": "50%",
        }

        msg = format_signal_message(signal, tier=UserTier.PRO)

        assert "$SOLAR" in msg
        assert "0.942 / 1.000" in msg
        assert "Tier: Pro" in msg
        assert "100% CLEAN" in msg
        assert "2.0x (+100%)" in msg
        assert "50% moonbag" in msg
        assert "Jito Bundle" in msg

    def test_sub_threshold_conviction_rejected(self):
        """Signals below 0.90 conviction score raise ValueError."""
        low_score_signal = {
            "symbol": "DOGE2",
            "mint": "So11111111111111111111111111111111111111112",
            "score": 0.885,  # < 0.90 threshold
        }

        with pytest.raises(ValueError, match="below ultra-conviction threshold"):
            format_signal_message(low_score_signal)

    def test_never_leak_simulated_paper_trade_data(self):
        """Rule 4: Prevent any simulated/paper-trade signals from leaking into alpha feed."""
        simulated_signals = [
            {"symbol": "SIM1", "mint": "abc", "score": 0.95, "is_paper": True},
            {"symbol": "SIM2", "mint": "abc", "score": 0.95, "is_simulated": True},
            {"symbol": "SIM3", "mint": "abc", "score": 0.95, "simulation": True},
        ]

        for s in simulated_signals:
            with pytest.raises(ValueError, match="Simulated paper trade data leak prevented"):
                format_signal_message(s)


# ==============================================================================
# 7. Copy-Trading Callbacks & 0.010 SOL Gas Floor Protection
# ==============================================================================

class TestCopyTradingAndGasFloorPreservation:
    """Test suite for interactive copy-trade callbacks and on-chain reserve preservation."""

    def test_callback_formatting_and_parsing(self):
        """Format and parse structured copy-trade callback strings."""
        mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

        formatted = format_copy_trade_callback("buy", mint, amount_sol=0.75, slippage_bps=150)
        assert formatted == f"copy:buy:{mint}:0.75:150"

        parsed = parse_copy_trade_callback(formatted)
        assert parsed["action"] == "buy"
        assert parsed["mint"] == mint
        assert parsed["amount_sol"] == 0.75
        assert parsed["slippage_bps"] == 150

        # Legacy shorthand support
        parsed_legacy = parse_copy_trade_callback(f"buy_{mint}")
        assert parsed_legacy["action"] == "buy"
        assert parsed_legacy["mint"] == mint
        assert parsed_legacy["amount_sol"] == 0.5
        assert parsed_legacy["slippage_bps"] == 100

    def test_copy_trade_keyboard_generation(self):
        """Generates inline keyboard with quick copy-trade amounts and DexScreener link."""
        mint = "So11111111111111111111111111111111111111112"
        kb = build_signal_keyboard(mint, amounts=[0.25, 0.5, 1.0], slippage_bps=100)

        buttons = kb["inline_keyboard"][0]
        assert len(buttons) == 3
        assert buttons[0]["text"] == "⚡ Copy 0.25 SOL"
        assert buttons[0]["callback_data"] == f"copy:buy:{mint}:0.25:100"
        assert buttons[1]["text"] == "⚡ Copy 0.5 SOL"
        assert buttons[2]["text"] == "⚡ Copy 1.0 SOL"

        row2 = kb["inline_keyboard"][1]
        assert row2[0]["url"] == f"https://dexscreener.com/solana/{mint}"

    def test_copy_trade_execution_preserves_0_010_sol_gas_reserve_floor(self):
        """Strictly enforce Rule 4: Preserve the 0.010 SOL gas reserve floor."""
        bot = TelegramAlphaBot()
        mint = str(Pubkey.new_unique())

        # Scenario A: User wallet balance 0.505 SOL, attempts to copy-trade 0.500 SOL
        # 0.505 - 0.500 = 0.005 SOL < 0.010 SOL floor -> MUST BE REJECTED
        result_fail = bot.execute_copy_trade(
            chat_id="user_poor",
            mint=mint,
            amount_sol=0.500,
            wallet_balance_sol=0.505,
        )
        assert result_fail["status"] == "REJECTED"
        assert result_fail["gas_floor_preserved"] is False
        assert "Gas reserve violation" in result_fail["reason"]

        # Scenario B: User wallet balance 1.000 SOL, attempts to copy-trade 0.500 SOL
        # 1.000 - 0.500 = 0.500 SOL > 0.010 SOL floor -> LANDS WITH CRYPTOGRAPHIC PROOF
        result_ok = bot.execute_copy_trade(
            chat_id="user_funded",
            mint=mint,
            amount_sol=0.500,
            wallet_balance_sol=1.000,
        )
        assert result_ok["status"] == "LANDED"
        assert result_ok["gas_floor_preserved"] is True
        assert len(result_ok["tx_hash"]) == 64  # Valid SHA-256 cryptographic hash
        assert len(result_ok["jito_bundle_id"]) == 32
        assert result_ok["remaining_balance_sol"] > 0.010

    def test_copy_trade_enforces_tier_size_limit(self):
        """Free tier cannot copy-trade above 1.0 SOL limit."""
        bot = TelegramAlphaBot()
        mint = str(Pubkey.new_unique())

        # Free tier limit is 1.0 SOL
        free_user = bot.register_user(chat_id="free_guy", username="FreeGuy")
        assert free_user.tier == UserTier.FREE

        res_reject = bot.execute_copy_trade(
            chat_id="free_guy",
            mint=mint,
            amount_sol=2.5,
            wallet_balance_sol=10.0,
        )
        assert res_reject["status"] == "REJECTED"
        assert "exceeds tier limit" in res_reject["reason"]

        # Upgrade to Pro (10.0 SOL limit)
        bot.set_user_tier("free_guy", UserTier.PRO)
        res_accept = bot.execute_copy_trade(
            chat_id="free_guy",
            mint=mint,
            amount_sol=2.5,
            wallet_balance_sol=10.0,
        )
        assert res_accept["status"] == "LANDED"

    def test_copy_trade_callback_handler_integration(self):
        """End-to-end callback handler parsing and execution."""
        bot = TelegramAlphaBot()
        mint = str(Pubkey.new_unique())

        # Referee (Trader) referred by Referrer
        bot.register_user("ref_boss", "Boss")
        bot.register_user("trader_sub", "TraderSub", referral_code="REF_ref_boss")

        callback_str = f"copy:buy:{mint}:0.5:100"
        exec_res = bot.handle_copy_trade_callback(
            chat_id="trader_sub",
            callback_data=callback_str,
            wallet_balance_sol=2.0,
        )

        assert exec_res["status"] == "LANDED"
        assert exec_res["amount_sol"] == 0.5
        assert exec_res["revenue_share"]["attributed"] is True
        assert exec_res["revenue_share"]["referrer_code"] == "REF_ref_boss"


# ==============================================================================
# 8. Headless API App Mounting Verification
# ==============================================================================

class TestHeadlessApiAppMount:
    """Verify monetization endpoints are mounted in create_headless_api_app."""

    def test_monetization_endpoints_registered_in_headless_app(
        self, headless_client, monkeypatch, sample_pubkeys
    ):
        """Sentinel and Reclaim routes respond via create_headless_api_app."""
        mint = sample_pubkeys["clean_mint"]

        def mock_rpc(method, params):
            return {
                "result": {
                    "value": {
                        "owner": str(sentinel_module.SPL_TOKEN_PROGRAM_ID),
                        "data": {
                            "parsed": {
                                "info": {
                                    "freezeAuthority": None,
                                    "mintAuthority": None,
                                    "extensions": [],
                                }
                            }
                        },
                    }
                }
            }

        monkeypatch.setattr(sentinel_module, "_rpc", mock_rpc)

        resp = headless_client.get(f"/api/v1/sentinel/scan/{mint}")
        assert resp.status_code == 200
        assert resp.json()["security_score"] == 100
