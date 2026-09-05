"""Funding-source resolution and the clustering policy built on top of it.

Three failures are possible here and none of them raises.

An unresolved funder leaves `funded_by` at `None` for every holder, which makes
`funder_graph_dispersion` report one independent actor per address — the
address-level count it was written to replace — and makes
`holder_distribution_health` compute its cluster Gini over clusters of size one.

A *wrongly* resolved funder is worse. Collapse ten unrelated customers of one
exchange into a single node and a well-distributed token reads as a sybil fleet,
so the metric is not merely uninformative, it is confidently backwards on the
launches it is most important to get right.

And a cached negative that came from a dead endpoint, rather than from the
chain, is a permanent lie: the table says "this wallet has no funder" and
nothing ever asks again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from botsensai.config import load_settings
from botsensai.metrics.base import MetricContext
from botsensai.metrics.topology import FunderGraphDispersion, HolderDistributionHealth
from botsensai.models import HolderRecord, TokenRef, utcnow
from botsensai.onchain.funding import (
    EXCHANGE_WALLETS,
    KIND_EXCHANGE,
    KIND_UNKNOWN,
    KIND_WALLET,
    FundingIndex,
    FundingSource,
    FundingSourceResolver,
    funder_from_transaction,
)
from botsensai.store.db import Database

TOKEN = TokenRef(mint="FundingTestMint11111111111111111111111111111")
CEX = next(iter(EXCHANGE_WALLETS))
PRIVATE_FUNDER = "PrivateFunder1111111111111111111111111111111"


def _holder(wallet: str, share: float, as_of: datetime | None = None) -> HolderRecord:
    when = as_of or utcnow()
    return HolderRecord(
        token=TOKEN,
        as_of=when,
        observed_at=when,
        wallet=wallet,
        balance=share * 1_000_000.0,
        share_of_supply=share,
    )


def _holders(n: int = 10) -> list[HolderRecord]:
    return [_holder(f"HOLDER_{i}", 1.0 / n) for i in range(n)]


def _cache(db: Database, wallet: str, funder: str | None, kind: str, **kw) -> None:
    db.upsert_wallet_funding(
        [
            FundingSource(
                wallet=wallet,
                funder=funder,
                kind=kind,
                exchange=kw.get("exchange"),
                funded_at=kw.get("funded_at"),
                signature=kw.get("signature", f"sig-{wallet}"),
            ).as_row()
        ]
    )


def _context(holders: list[HolderRecord]) -> MetricContext:
    return MetricContext(token=TOKEN, as_of=utcnow(), holders=holders)


class _ScriptedRpc:
    """A PacedClient stand-in that answers JSON-RPC from a script.

    Keyed on method, so a test says what `getSignaturesForAddress` returns
    without caring how the resolver spells the request. Every call is recorded:
    the cache is only worth having if it stops calls being made, and counting
    them is the only way to assert that.
    """

    def __init__(self, **responses) -> None:
        self.responses = responses
        self.calls: list[tuple[str, list]] = []

    async def post_json(self, url: str, payload: dict) -> dict:
        method = payload["method"]
        self.calls.append((method, payload["params"]))
        answer = self.responses.get(method)
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer(payload["params"])
        return {"jsonrpc": "2.0", "id": 1, "result": answer}

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    async def aclose(self) -> None:
        return None


def _resolver(**responses) -> tuple[FundingSourceResolver, _ScriptedRpc]:
    rpc = _ScriptedRpc(**responses)
    resolver = FundingSourceResolver(load_settings(), client=rpc)  # type: ignore[arg-type]
    return resolver, rpc


def _signature_rows(*signatures: str, block_time: int = 1_760_000_000) -> list[dict]:
    """Newest-first, the way the RPC returns them."""
    return [
        {"signature": sig, "blockTime": block_time - i, "err": None}
        for i, sig in enumerate(signatures)
    ]


def _transfer_tx(source: str, destination: str, lamports: int = 50_000_000) -> dict:
    return {
        "blockTime": 1_760_000_000,
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": source}, {"pubkey": destination}],
                "instructions": [
                    {
                        "program": "system",
                        "parsed": {
                            "type": "transfer",
                            "info": {
                                "source": source,
                                "destination": destination,
                                "lamports": lamports,
                            },
                        },
                    }
                ],
            }
        },
        "meta": {"preBalances": [10**9, 0], "postBalances": [10**9 - lamports, lamports]},
    }


# --------------------------------------------------------------------------- #
# the clustering policy — the reason this task exists
# --------------------------------------------------------------------------- #


def test_a_shared_cex_funder_does_not_merge_two_independent_holders():
    """Two people who both withdrew from Binance are two people."""
    db = Database(":memory:")
    holders = _holders(10)
    _cache(db, "HOLDER_0", CEX, KIND_EXCHANGE, exchange=EXCHANGE_WALLETS[CEX])
    _cache(db, "HOLDER_1", CEX, KIND_EXCHANGE, exchange=EXCHANGE_WALLETS[CEX])

    applied = FundingIndex(db).apply(holders)
    by_wallet = {h.wallet: h for h in applied}

    assert by_wallet["HOLDER_0"].funded_by is None
    assert by_wallet["HOLDER_1"].funded_by is None
    # The fact is not erased, only withheld from clustering.
    assert "cex:binance" in by_wallet["HOLDER_0"].labels

    graph = FunderGraphDispersion._funding_graph(applied)
    assert not graph.has_edge("HOLDER_0", f"funder:{CEX}")
    value, _, _ = FunderGraphDispersion().compute(_context(applied))
    assert value == pytest.approx(10.0), "ten holders, ten independent actors"


def test_a_shared_private_funder_does_merge_two_holders():
    """The positive control: without it the test above passes for the wrong reason."""
    db = Database(":memory:")
    holders = _holders(10)
    for wallet in ("HOLDER_0", "HOLDER_1", "HOLDER_2"):
        _cache(db, wallet, PRIVATE_FUNDER, KIND_WALLET)

    applied = FundingIndex(db).apply(holders)
    by_wallet = {h.wallet: h for h in applied}
    assert by_wallet["HOLDER_0"].funded_by == PRIVATE_FUNDER

    value, _, _ = FunderGraphDispersion().compute(_context(applied))
    assert value is not None and value < 9.0, "three wallets on one funder is one actor"


def test_the_cluster_gini_sees_the_exchange_case_the_same_way():
    """`holder_distribution_health` clusters on `funded_by` with no degree guard.

    So it is the metric that a mislabelled exchange damages most, and it has to
    be checked separately from the graph metric rather than assumed to follow.
    """
    db = Database(":memory:")
    holders = [_holder(f"HOLDER_{i}", 1.0 / 10) for i in range(10)]
    for wallet in ("HOLDER_0", "HOLDER_1", "HOLDER_2", "HOLDER_3"):
        _cache(db, wallet, CEX, KIND_EXCHANGE, exchange=EXCHANGE_WALLETS[CEX])

    healthy, _, note = HolderDistributionHealth().compute(_context(FundingIndex(db).apply(holders)))
    assert "10 clusters" in note

    for wallet in ("HOLDER_0", "HOLDER_1", "HOLDER_2", "HOLDER_3"):
        _cache(db, wallet, PRIVATE_FUNDER, KIND_WALLET)
    index = FundingIndex(db)
    disguised, _, _ = HolderDistributionHealth().compute(_context(index.apply(holders)))

    assert healthy is not None and disguised is not None
    assert disguised < healthy, "four wallets behind one private funder is concentration"


def test_a_high_fanout_funder_is_treated_as_a_dispenser_even_unlabelled():
    """The measured half of the rule: the seed list can never be complete."""
    db = Database(":memory:")
    unlisted = "UnlistedExchange111111111111111111111111111"
    for i in range(30):
        _cache(db, f"CUSTOMER_{i}", unlisted, KIND_WALLET)
    _cache(db, "HOLDER_0", unlisted, KIND_WALLET)
    _cache(db, "HOLDER_1", unlisted, KIND_WALLET)

    index = FundingIndex(db, fanout_threshold=25)
    applied = index.apply(_holders(10))
    assert all(h.funded_by is None for h in applied)
    assert index.stats()["dispensers"] == 1

    # Below its own threshold the same data clusters, so the threshold is what
    # is being tested and not some other exclusion.
    lenient = FundingIndex(db, fanout_threshold=100)
    assert lenient.apply(_holders(10))[0].funded_by == unlisted


def test_funding_after_the_scoring_instant_is_not_applied():
    """Event-time bound. A later transfer is not evidence available to the decision."""
    db = Database(":memory:")
    when = utcnow()
    _cache(db, "HOLDER_0", PRIVATE_FUNDER, KIND_WALLET, funded_at=when + timedelta(hours=1))
    _cache(db, "HOLDER_1", PRIVATE_FUNDER, KIND_WALLET, funded_at=when - timedelta(hours=1))

    index = FundingIndex(db)
    funders = index.funders_for(["HOLDER_0", "HOLDER_1"], before=when)
    assert "HOLDER_0" not in funders
    assert funders["HOLDER_1"] == PRIVATE_FUNDER


# --------------------------------------------------------------------------- #
# resolution over RPC
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_oldest_successful_signature_is_the_one_fetched():
    """First funding is the *oldest* transaction; the RPC returns newest first."""
    resolver, rpc = _resolver(
        getSignaturesForAddress=_signature_rows("newest", "middle", "oldest"),
        getTransaction=_transfer_tx(PRIVATE_FUNDER, "WALLET_A"),
    )
    source = await resolver.resolve_one("WALLET_A")

    assert rpc.calls[1][1][0] == "oldest"
    assert source.funder == PRIVATE_FUNDER
    assert source.kind == KIND_WALLET
    assert source.funded_at is not None and source.funded_at.tzinfo is not None


@pytest.mark.asyncio
async def test_a_failed_transaction_cannot_be_the_funding_transaction():
    rows = _signature_rows("newest", "oldest")
    rows[-1]["err"] = {"InstructionError": [0, "Custom"]}
    resolver, rpc = _resolver(
        getSignaturesForAddress=rows,
        getTransaction=_transfer_tx(PRIVATE_FUNDER, "WALLET_A"),
    )
    await resolver.resolve_one("WALLET_A")
    assert rpc.calls[1][1][0] == "newest", "the failed older signature moved no lamports"


@pytest.mark.asyncio
async def test_a_wallet_with_more_history_than_one_page_is_unresolved_not_unfunded():
    resolver, rpc = _resolver(
        getSignaturesForAddress=_signature_rows(*[f"sig{i}" for i in range(1000)])
    )
    source = await resolver.resolve_one("WALLET_BUSY")

    assert source.kind == KIND_UNKNOWN
    assert source.funder is None
    assert source.source == "rpc:history-exceeds-one-page"
    assert rpc.methods() == ["getSignaturesForAddress"], "no second call is worth making"


@pytest.mark.asyncio
async def test_an_exchange_funder_is_classified_at_resolution_time():
    resolver, _ = _resolver(
        getSignaturesForAddress=_signature_rows("only"),
        getTransaction=_transfer_tx(CEX, "WALLET_A"),
    )
    source = await resolver.resolve_one("WALLET_A")
    assert source.kind == KIND_EXCHANGE
    assert source.exchange == EXCHANGE_WALLETS[CEX]
    assert source.funder == CEX, "the fact is kept; only the clustering is withheld"


def test_the_funder_is_found_in_an_inner_instruction():
    """CEX withdrawals and router hops are CPIs, not top-level transfers."""
    tx = {
        "transaction": {"message": {"accountKeys": [], "instructions": []}},
        "meta": {
            "innerInstructions": [
                {
                    "index": 0,
                    "instructions": [
                        {
                            "parsed": {
                                "type": "transfer",
                                "info": {"source": CEX, "destination": "WALLET_A"},
                            }
                        }
                    ],
                }
            ]
        },
    }
    assert funder_from_transaction(tx, "WALLET_A") == (CEX, "solana_rpc:transfer")


def test_the_balance_delta_fallback_finds_an_unparsed_funder():
    tx = {
        "transaction": {
            "message": {
                "accountKeys": [
                    {"pubkey": "SPENDER_SMALL"},
                    {"pubkey": PRIVATE_FUNDER},
                    {"pubkey": "WALLET_A"},
                ],
                "instructions": [{"programId": "SomeProgram", "accounts": []}],
            }
        },
        "meta": {
            "preBalances": [10**9, 10**9, 0],
            "postBalances": [10**9 - 5_000, 10**9 - 900_000, 900_000],
        },
    }
    assert funder_from_transaction(tx, "WALLET_A") == (
        PRIVATE_FUNDER,
        "solana_rpc:balance-delta",
    )


def test_the_payer_of_the_first_token_account_counts_as_the_funder():
    """Shape taken from a real holder's oldest transaction (probe, 2026-08-03).

    The wallet's lamport balance does not move at all: someone else pays the
    rent to open its associated token account and sends it the token. Reading
    only SOL movements files this wallet as unfunded, which is how the funding
    graph ends up empty on precisely the airdropped-fleet case it exists for.
    """
    ata = "HX6KJXNgTbzGMhWTBsDJvYw8hCM63pZ1zwcydA83aNKj"
    tx = {
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": PRIVATE_FUNDER}, {"pubkey": "WALLET_A"}],
                "instructions": [
                    {
                        "program": "spl-associated-token-account",
                        "parsed": {
                            "type": "createIdempotent",
                            "info": {
                                "account": ata,
                                "source": PRIVATE_FUNDER,
                                "wallet": "WALLET_A",
                            },
                        },
                    }
                ],
            }
        },
        "meta": {"preBalances": [10**9, 5_000], "postBalances": [10**9 - 2_074_080, 5_000]},
    }
    assert funder_from_transaction(tx, "WALLET_A") == (
        PRIVATE_FUNDER,
        "solana_rpc:account-payer",
    )


def test_a_transaction_that_did_not_credit_the_wallet_has_no_funder():
    tx = {
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": PRIVATE_FUNDER}, {"pubkey": "WALLET_A"}],
                "instructions": [{"programId": "SomeProgram"}],
            }
        },
        "meta": {"preBalances": [10**9, 5_000], "postBalances": [10**9, 4_000]},
    }
    assert funder_from_transaction(tx, "WALLET_A") == (None, "rpc:no-inbound-transfer")


# --------------------------------------------------------------------------- #
# the cache
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_resolved_answer_is_never_asked_for_twice():
    """The cache is the whole reason this fits inside the RPC budget."""
    db = Database(":memory:")
    resolver, rpc = _resolver(
        getSignaturesForAddress=_signature_rows("only"),
        getTransaction=_transfer_tx(PRIVATE_FUNDER, "WALLET_A"),
    )
    index = FundingIndex(db, resolver=resolver)

    assert await index.resolve_missing(["WALLET_A"]) == 1
    calls_after_first = len(rpc.calls)
    assert await index.resolve_missing(["WALLET_A"]) == 0
    assert len(rpc.calls) == calls_after_first

    # A different index instance, i.e. a later sweep, reads it from the store.
    assert FundingIndex(db).funders_for(["WALLET_A"]) == {"WALLET_A": PRIVATE_FUNDER}


@pytest.mark.asyncio
async def test_a_transport_failure_is_not_cached_as_absence_of_funding():
    """A dead endpoint must not be written into the store as a fact about a wallet."""
    db = Database(":memory:")
    resolver, _ = _resolver(getSignaturesForAddress=RuntimeError("503 from the RPC node"))
    index = FundingIndex(db, resolver=resolver)

    assert await index.resolve_missing(["WALLET_A"]) == 0
    assert db.wallet_funding(["WALLET_A"]) == {}
    assert index.unresolved(["WALLET_A"]) == ["WALLET_A"], "it must be retried later"


@pytest.mark.asyncio
async def test_a_chain_answered_negative_is_cached():
    """"We looked and there is nothing" is an answer, and re-asking cannot change it."""
    db = Database(":memory:")
    resolver, rpc = _resolver(getSignaturesForAddress=[])
    index = FundingIndex(db, resolver=resolver)

    assert await index.resolve_missing(["WALLET_NEW"]) == 1
    assert db.wallet_funding(["WALLET_NEW"])["WALLET_NEW"]["kind"] == KIND_UNKNOWN
    assert index.unresolved(["WALLET_NEW"]) == []
    assert len(rpc.calls) == 1


@pytest.mark.asyncio
async def test_the_batch_stops_at_its_budget():
    db = Database(":memory:")
    resolver, rpc = _resolver(
        getSignaturesForAddress=_signature_rows("only"),
        getTransaction=_transfer_tx(PRIVATE_FUNDER, "W"),
    )
    index = FundingIndex(db, resolver=resolver)

    written = await index.resolve_missing([f"WALLET_{i}" for i in range(20)], limit=3)
    assert written == 3
    assert len(rpc.calls) == 6, "two calls per wallet, three wallets, nothing more"


@pytest.mark.asyncio
async def test_an_index_with_no_resolver_still_serves_the_cache():
    """Backtests and offline runs read funding; they never resolve it."""
    db = Database(":memory:")
    _cache(db, "HOLDER_0", PRIVATE_FUNDER, KIND_WALLET)
    index = FundingIndex(db)

    assert await index.resolve_missing(["HOLDER_9"]) == 0
    assert index.funders_for(["HOLDER_0"]) == {"HOLDER_0": PRIVATE_FUNDER}


# --------------------------------------------------------------------------- #
# the pipeline seam
# --------------------------------------------------------------------------- #


def test_build_context_attaches_funders_to_stored_holders(tmp_path):
    """The store holds no `funded_by`; the context is where it is joined on."""
    from botsensai.pipeline import Pipeline

    settings = load_settings()
    settings.db_path = str(tmp_path / "funding.db")
    db = Database(settings.db_path)
    pipeline = Pipeline(settings, db=db, collectors=[])

    created = utcnow() - timedelta(minutes=30)
    holders = [_holder(f"HOLDER_{i}", 1.0 / 10, as_of=created + timedelta(minutes=5)) for i in range(10)]
    db.insert_holders(holders)
    assert all(h["funded_by"] is None for h in [dict(r) for r in db.conn.execute("SELECT * FROM holders")])

    _cache(db, "HOLDER_0", PRIVATE_FUNDER, KIND_WALLET)
    _cache(db, "HOLDER_1", CEX, KIND_EXCHANGE, exchange=EXCHANGE_WALLETS[CEX])

    launch = _launch(created)
    db.upsert_launch(launch)
    ctx = pipeline.build_context(launch)
    by_wallet = {h.wallet: h for h in ctx.holders}

    assert by_wallet["HOLDER_0"].funded_by == PRIVATE_FUNDER
    assert by_wallet["HOLDER_1"].funded_by is None
    assert "cex:binance" in by_wallet["HOLDER_1"].labels


@pytest.mark.asyncio
async def test_enrich_never_fails_because_funding_resolution_did(tmp_path):
    from botsensai.pipeline import Pipeline

    settings = load_settings()
    settings.db_path = str(tmp_path / "enrich.db")
    pipeline = Pipeline(settings, db=Database(settings.db_path), collectors=[])
    resolver, _ = _resolver(getSignaturesForAddress=RuntimeError("connection reset"))
    pipeline.funding = FundingIndex(pipeline.db, settings, resolver=resolver)

    assert await pipeline._resolve_funding(_holders(4)) == 0
    assert await pipeline.enrich([]) is not None


def _launch(created_at: datetime):
    from botsensai.models import Launch

    return Launch(
        token=TOKEN,
        created_at=created_at,
        observed_at=created_at,
        launchpad="pumpfun",
        deployer="DEPLOYER_1111111111111111111111111111111111",
        source="test",
    )


def test_the_seed_exchange_list_is_plausible_base58():
    """A typo in the seed list silently disables one exchange's exclusion."""
    alphabet = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    for address, name in EXCHANGE_WALLETS.items():
        assert 32 <= len(address) <= 44, address
        assert set(address) <= alphabet, address
        assert name and name.islower(), name


def test_a_funding_row_survives_a_round_trip(tmp_path):
    db = Database(str(tmp_path / "roundtrip.db"))
    funded_at = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    db.upsert_wallet_funding(
        [
            FundingSource(
                wallet="W",
                funder=PRIVATE_FUNDER,
                kind=KIND_WALLET,
                funded_at=funded_at,
                signature="sig",
            ).as_row()
        ]
    )
    source = FundingIndex(db).sources_for(["W"])["W"]
    assert source.funder == PRIVATE_FUNDER
    assert source.funded_at == funded_at
    assert source.signature == "sig"
