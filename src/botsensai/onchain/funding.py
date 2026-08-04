"""Who paid for each wallet, and which of those answers may be clustered on.

`HolderRecord.funded_by` is the input to `funder_graph_dispersion`,
`holder_distribution_health` and the deployer-cluster exclusions in
`early_holder_retention` and the credibility family. Nothing populated it live,
so every one of those ran on `None` for every holder: the funding graph had no
edges, and the cluster Gini was computed over one cluster per address, which is
the address-level number those metrics exist specifically to replace.

Three things make this module more than an RPC wrapper.

**The answer is immutable, so the cache is permanent.** A wallet has exactly one
oldest transaction. Once resolved it is stored in `wallet_funding` and never
looked up again — which matters, because the resolution costs two RPC calls
against the scarcest budget in the system.

**"Funded" is broader than "sent SOL".** The first thing that happens to a
holder wallet is often not a transfer but somebody paying rent to open its
token account and filling it, leaving its lamport balance untouched. Reading
only SOL movements leaves those wallets looking unfunded — measured at 2 of the
first 13 real wallets probed — so three readings are tried in descending order
of strength and the one that answered is recorded alongside the funder.

**`funded_at` bounds it, `resolved_at` does not.** Everything else in the store
carries a knowledge bound because it is sampled from a stream: whether we had
collected a trade by the decision point genuinely varies. Funding is a point
lookup on a wallet already in hand, and the funding necessarily precedes the
wallet's first buy, so the same call at the decision point would have returned
the same answer. Bounding on when *we* got around to asking would delete the
feature from every backtest without making it any more honest. The event-time
bound (`funded_at < before`) is enforced; the collection lag is not.

**A shared exchange funder is not a cluster.** Ten wallets that withdrew from
Binance are ten independent people. Collapsing them into one node inflates
cluster concentration for exactly the tokens with the healthiest distribution,
and `holder_distribution_health` clusters on `funded_by` with no degree guard at
all. So an exchange funder is recorded as a fact and withheld as a cluster key:
the holder keeps a `cex:<name>` label and `funded_by` stays `None`.

Two mechanisms decide that, because either alone is insufficient:

* `EXCHANGE_WALLETS` — a seed list of hot wallets, from public block-explorer
  labels. It is not verified in this repository and it cannot be complete.
* `Database.funder_fanout` — measured against our own corpus. A funder standing
  behind `DEFAULT_FANOUT_THRESHOLD` distinct wallets is a dispenser whatever it
  is labelled, and this needs no trust in anyone's tags.

Both err in the same direction. Treating a real distributor as an exchange
loses a signal; the reverse manufactures one. Only the first is recoverable, so
that is the way the mistake is made.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from botsensai.config import Settings, get_settings
from botsensai.models import HolderRecord
from botsensai.store.db import Database
from botsensai.util.http import PacedClient
from botsensai.util.logging import get_logger

log = get_logger(__name__)

#: Exchange hot wallets, keyed by address. Sourced from public block-explorer
#: labels and **not** verified live by this repository — which is survivable
#: precisely because the fanout rule below is the load-bearing half and this is
#: only the day-one seed, before the corpus has enough wallets to measure.
#: Extend at runtime through `collectors.solana_rpc.extra.exchange_wallets`.
EXCHANGE_WALLETS: dict[str, str] = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "binance",
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S": "binance",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "binance",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "coinbase",
    "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE": "coinbase",
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": "kraken",
    "5VCwKtCXgCJ6kit5FybXjvriW3xELsFDhYrPSqtJNmcD": "okx",
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2": "bybit",
    "BmFdpraQhkiDQE6SnfG5omcA1VwzqfXrwtNYBwWTymy6": "kucoin",
    "u6PJ8DtQuPFnfmwHbGFULQ4u4EgjDiyYKjVEsynXq2w": "gate",
    "ASTyfSima4LLAdDgoFGkgqoKowG1LZFDr9fAQrg7iaJZ": "mexc",
}

#: Distinct wallets a funder must stand behind, in our own store, before it is
#: treated as a dispenser rather than a cluster. A private distributor funding a
#: single launch's fleet is well under this; an exchange passes it within days.
DEFAULT_FANOUT_THRESHOLD = 25

#: Signatures asked for in one `getSignaturesForAddress` page. A wallet with
#: more history than this is not paginated to the end: the walk costs one call
#: per page against a budget measured in tens of calls per sweep, and a wallet
#: with a thousand transactions is established history, which is the population
#: these metrics are least interested in. It is recorded as unresolved, not as
#: unfunded.
SIGNATURE_PAGE = 1000

#: Wallets resolved in one sweep, and the wall-clock ceiling on doing it. Both
#: exist because resolution is inline with `enrich`: a sweep that spends three
#: minutes on funding has missed the decision window it was collecting for.
DEFAULT_RESOLVE_LIMIT = 40
DEFAULT_RESOLVE_DEADLINE_SECONDS = 20.0

#: Never a funder: the system program itself and the addresses that appear as a
#: counterparty for protocol reasons rather than because someone paid someone.
NON_FUNDERS = frozenset(
    {
        "11111111111111111111111111111111",
        "1nc1nerator11111111111111111111111111111111",
        "SysvarRent111111111111111111111111111111111",
        "ComputeBudget111111111111111111111111111111",
    }
)

KIND_WALLET = "wallet"
KIND_EXCHANGE = "exchange"
KIND_UNKNOWN = "unknown"


@dataclass(frozen=True)
class FundingSource:
    """Who first paid for a wallet, and how confidently we can say so.

    `source` carries the mechanism — a parsed transfer, the payer of the
    wallet's first token account, or a lamport-delta inference — so a cluster
    built on the weakest reading can be told apart later from one the chain
    stated outright.
    """

    wallet: str
    funder: str | None = None
    kind: str = KIND_UNKNOWN
    exchange: str | None = None
    funded_at: datetime | None = None
    signature: str | None = None
    source: str = "solana_rpc"

    def as_row(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "funder": self.funder,
            "kind": self.kind,
            "exchange": self.exchange,
            "funded_at": self.funded_at,
            "signature": self.signature,
            "source": self.source,
        }


def classify_funder(
    funder: str | None, exchanges: dict[str, str] | None = None
) -> tuple[str, str | None]:
    """`(kind, exchange_name)` for a resolved funder address."""
    table = EXCHANGE_WALLETS if exchanges is None else exchanges
    if not funder or funder in NON_FUNDERS:
        return KIND_UNKNOWN, None
    name = table.get(funder)
    if name:
        return KIND_EXCHANGE, name
    return KIND_WALLET, None


# --------------------------------------------------------------------------- #
# transaction parsing
# --------------------------------------------------------------------------- #


def _instructions(tx: dict[str, Any]) -> list[dict[str, Any]]:
    """Top-level and inner instructions, flattened.

    Funding routinely arrives as an inner instruction — a CEX withdrawal or a
    router hop is a CPI, not a bare system transfer — so reading only the
    top-level list misses the case this module exists for.
    """
    message = (tx.get("transaction") or {}).get("message") or {}
    out: list[dict[str, Any]] = list(message.get("instructions") or [])
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        out.extend(group.get("instructions") or [])
    return out


def _transfer_funder(tx: dict[str, Any], wallet: str) -> str | None:
    """Source of the first parsed system transfer that credits `wallet`."""
    for instruction in _instructions(tx):
        parsed = instruction.get("parsed")
        if not isinstance(parsed, dict):
            continue
        info = parsed.get("info") or {}
        target = info.get("destination") or info.get("newAccount")
        if target != wallet:
            continue
        source = info.get("source") or info.get("lamportsSource")
        if isinstance(source, str) and source != wallet and source not in NON_FUNDERS:
            return source
    return None


def _account_payer_funder(tx: dict[str, Any], wallet: str) -> str | None:
    """Whoever paid to bring the wallet's first token account into existence.

    Found by probing the live corpus: a holder's oldest transaction is often not
    a SOL transfer at all but somebody creating its associated token account and
    sending it tokens, with the *sender* paying the rent. The wallet's own
    lamport balance never moves, so both of the checks above miss it and the
    wallet reads as having no funder — 2 of the first 13 real wallets probed.

    That payer is a stronger cluster edge than a SOL transfer, not a weaker one:
    paying rent to open an account for someone and filling it with a token is a
    deliberate act toward that specific wallet. An indiscriminate airdropper
    doing it to thousands of strangers is caught by the fanout rule, which is
    where that case belongs.
    """
    for instruction in _instructions(tx):
        parsed = instruction.get("parsed")
        if not isinstance(parsed, dict):
            continue
        info = parsed.get("info") or {}
        owner = info.get("wallet") or info.get("owner")
        source = info.get("source")
        if owner != wallet or not isinstance(source, str):
            continue
        if source != wallet and source not in NON_FUNDERS:
            return source
    return None


def _account_keys(tx: dict[str, Any]) -> list[str]:
    message = (tx.get("transaction") or {}).get("message") or {}
    keys: list[str] = []
    for key in message.get("accountKeys") or []:
        if isinstance(key, dict):
            pubkey = key.get("pubkey")
            if isinstance(pubkey, str):
                keys.append(pubkey)
        elif isinstance(key, str):
            keys.append(key)
    return keys


def _balance_funder(tx: dict[str, Any], wallet: str) -> str | None:
    """Fallback: whoever lost the most lamports in the transaction that credited us.

    Used when the instruction is not one the RPC node parses (a program's own
    transfer, an older encoding). It is a weaker claim than a parsed transfer,
    so it is only reached after that fails, and it still requires the wallet's
    own balance to have risen.
    """
    meta = tx.get("meta") or {}
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    keys = _account_keys(tx)
    if not keys or len(pre) != len(keys) or len(post) != len(keys):
        return None
    try:
        index = keys.index(wallet)
    except ValueError:
        return None
    if post[index] <= pre[index]:
        return None

    spender, spent = None, 0
    for i, key in enumerate(keys):
        if i == index or key in NON_FUNDERS:
            continue
        delta = pre[i] - post[i]
        if delta > spent:
            spender, spent = key, delta
    return spender


def funder_from_transaction(tx: dict[str, Any], wallet: str) -> tuple[str | None, str]:
    """Who paid for `wallet` in this transaction, and by which reading.

    Returned with the mechanism because they are not equally strong claims and
    the store keeps the distinction: a parsed transfer is the chain saying so, a
    balance delta is an inference from who lost the most lamports. A funding
    graph built mostly out of inferences deserves to be read differently from
    one built out of transfers, and that is only visible if it is recorded.
    """
    if not isinstance(tx, dict):
        return None, "rpc:no-transaction"
    for mechanism, finder in (
        ("transfer", _transfer_funder),
        ("account-payer", _account_payer_funder),
        ("balance-delta", _balance_funder),
    ):
        funder = finder(tx, wallet)
        if funder:
            return funder, f"solana_rpc:{mechanism}"
    return None, "rpc:no-inbound-transfer"


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #


class FundingSourceResolver:
    """Resolves a wallet's first funder over Solana JSON-RPC.

    Two calls per wallet: the oldest signature the address has, then that
    transaction. A wallet the chain answers for but that has no identifiable
    funder is a *resolved negative* — `KIND_UNKNOWN`, with the reason in
    `source` — and is cached, because asking again would get the same answer. A
    transport failure is not: those wallets are left out of the batch entirely
    so a dead endpoint cannot be written into the store as absence of funding.
    """

    surface = "solana_rpc"

    def __init__(
        self,
        settings: Settings | None = None,
        client: PacedClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.config = self.settings.collector(self.surface)
        self._client = client
        self.calls = 0

    @property
    def rpc_url(self) -> str:
        key = self.settings.helius_api_key
        if key:
            return f"https://mainnet.helius-rpc.com/?api-key={key}"
        return self.settings.solana_rpc_url

    @property
    def client(self) -> PacedClient:
        if self._client is None:
            self._client = PacedClient(
                self.surface,
                requests_per_minute=self.config.requests_per_minute,
                max_concurrency=self.config.max_concurrency,
                timeout=self.config.timeout_seconds,
                max_retries=self.config.max_retries,
                # Two identical JSON-RPC POSTs are never issued in one sweep,
                # and the answer is cached in the store anyway.
                cache_ttl=0.0,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        self.calls += 1
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        response = await self.client.post_json(self.rpc_url, payload)
        if not isinstance(response, dict):
            raise ValueError(f"{method}: non-object response")
        error = response.get("error")
        if error:
            raise ValueError(f"{method}: {error}")
        return response.get("result")

    async def _oldest_signature(self, wallet: str) -> tuple[str | None, float | None, str]:
        """Oldest successful signature for `wallet`, and why if there is none."""
        rows = await self._rpc(
            "getSignaturesForAddress", [wallet, {"limit": SIGNATURE_PAGE}]
        )
        if not isinstance(rows, list) or not rows:
            return None, None, "rpc:no-signatures"
        if len(rows) >= SIGNATURE_PAGE:
            # Walking to the end costs a call per page; an address this busy is
            # established history rather than a fresh fleet member.
            return None, None, "rpc:history-exceeds-one-page"
        succeeded = [r for r in rows if isinstance(r, dict) and not r.get("err")]
        if not succeeded:
            return None, None, "rpc:no-successful-signatures"
        oldest = succeeded[-1]
        signature = oldest.get("signature")
        if not isinstance(signature, str):
            return None, None, "rpc:signature-missing"
        block_time = oldest.get("blockTime")
        return signature, float(block_time) if block_time else None, "solana_rpc"

    async def resolve_one(self, wallet: str) -> FundingSource:
        signature, block_time, note = await self._oldest_signature(wallet)
        if signature is None:
            return FundingSource(wallet=wallet, source=note)

        tx = await self._rpc(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )
        funder, mechanism = funder_from_transaction(tx if isinstance(tx, dict) else {}, wallet)
        kind, exchange = classify_funder(funder, self._exchange_table())
        return FundingSource(
            wallet=wallet,
            funder=funder if kind != KIND_UNKNOWN else None,
            kind=kind,
            exchange=exchange,
            funded_at=_dt_from_block_time(block_time),
            signature=signature,
            source=mechanism,
        )

    def _exchange_table(self) -> dict[str, str]:
        extra = self.config.extra.get("exchange_wallets")
        if isinstance(extra, dict):
            return {**EXCHANGE_WALLETS, **{str(k): str(v) for k, v in extra.items()}}
        return EXCHANGE_WALLETS

    async def resolve(
        self, wallets: Sequence[str], deadline_seconds: float | None = None
    ) -> list[FundingSource]:
        """Resolve in the order given, stopping at the deadline.

        Sequential on purpose. The public RPC endpoint is the tightest budget in
        the system and the caller has already trimmed the list to the wallets
        that matter most, so parallelism here would buy latency at the cost of
        the 429 that stops the next sweep too.
        """
        end = None if deadline_seconds is None else time.monotonic() + deadline_seconds
        out: list[FundingSource] = []
        for wallet in wallets:
            if end is not None and time.monotonic() >= end:
                log.info("funding.deadline", resolved=len(out), remaining=len(wallets) - len(out))
                break
            try:
                out.append(await self.resolve_one(wallet))
            except Exception as exc:  # noqa: BLE001 — a dead RPC must not be cached
                # Stop the batch rather than skipping the wallet: a failure here
                # is nearly always the endpoint rather than the address, and
                # marching through forty more wallets converts one error into a
                # rate-limit block that costs the next sweep as well.
                log.warning("funding.resolve_failed", wallet=wallet, error=str(exc))
                break
        return out


def _dt_from_block_time(block_time: float | None) -> datetime | None:
    if not block_time:
        return None
    return datetime.fromtimestamp(float(block_time), tz=UTC)


# --------------------------------------------------------------------------- #
# index
# --------------------------------------------------------------------------- #


@dataclass
class FundingIndex:
    """Store-backed funding lookup, plus the policy on what may be clustered.

    The split matters: `wallet_funding` records what we found, and this class
    decides what a metric is allowed to do with it. An exchange funder stays in
    the table — it is a true fact and the fanout measurement depends on it —
    and is simply never handed out as a cluster key.
    """

    db: Database
    settings: Settings | None = None
    resolver: FundingSourceResolver | None = None
    fanout_threshold: int = DEFAULT_FANOUT_THRESHOLD
    _fanout: dict[str, int] | None = field(default=None, init=False, repr=False)
    resolved: int = field(default=0, init=False)

    # -- reads -------------------------------------------------------------- #

    def _dispensers(self) -> dict[str, int]:
        if self._fanout is None:
            self._fanout = self.db.funder_fanout(self.fanout_threshold)
        return self._fanout

    def _is_dispenser(self, funder: str) -> bool:
        return funder in self._dispensers()

    def sources_for(self, wallets: Iterable[str]) -> dict[str, FundingSource]:
        """Everything we know about these wallets, exchange rows included."""
        rows = self.db.wallet_funding(wallets)
        return {wallet: _row_to_source(row) for wallet, row in rows.items()}

    def funders_for(
        self, wallets: Iterable[str], before: datetime | None = None
    ) -> dict[str, str]:
        """Cluster-safe funder per wallet. Exchanges and dispensers are omitted.

        `before` is an event-time bound: a funding transfer stamped after the
        moment being scored is not something the decision could have used, so it
        is dropped. An unknown `funded_at` is kept — the transfer necessarily
        precedes the wallet's first buy, which precedes this holder record.
        """
        out: dict[str, str] = {}
        for wallet, source in self.sources_for(wallets).items():
            funder = source.funder
            if not funder or source.kind != KIND_WALLET or self._is_dispenser(funder):
                continue
            if before is not None and source.funded_at is not None and source.funded_at >= before:
                continue
            out[wallet] = funder
        return out

    def apply(
        self, holders: Sequence[HolderRecord], before: datetime | None = None
    ) -> list[HolderRecord]:
        """Attach funders to holder records, labelling the exchange withdrawals.

        A holder funded from an exchange comes back with `funded_by` cleared and
        a `cex:<name>` label. That is the whole point: the label preserves the
        fact for anything that wants it, and the cleared field keeps ten
        unrelated Binance customers from reading as one actor.
        """
        if not holders:
            return list(holders)
        sources = self.sources_for(h.wallet for h in holders)
        safe = self.funders_for(sources.keys(), before)

        out: list[HolderRecord] = []
        for holder in holders:
            source = sources.get(holder.wallet)
            if source is None:
                out.append(holder)
                continue
            update: dict[str, Any] = {"funded_by": safe.get(holder.wallet)}
            label = _exchange_label(source)
            if label and label not in holder.labels:
                update["labels"] = [*holder.labels, label]
            out.append(holder.model_copy(update=update))
        return out

    # -- resolution --------------------------------------------------------- #

    def unresolved(self, wallets: Iterable[str]) -> list[str]:
        """Wallets with no cached answer, in the order given."""
        unique = list(dict.fromkeys(w for w in wallets if w))
        known = self.db.wallet_funding(unique)
        return [w for w in unique if w not in known]

    async def resolve_missing(
        self,
        wallets: Iterable[str],
        limit: int | None = None,
        deadline_seconds: float | None = None,
    ) -> int:
        """Resolve and cache the wallets we have never looked up. Never raises.

        The caller passes wallets in priority order (largest holdings first):
        the budget is small enough that which wallets get spent on it is a real
        decision, and the top of the holder table is where clustering is
        decided.
        """
        pending = self.unresolved(wallets)
        if not pending or self.resolver is None:
            return 0
        budget = DEFAULT_RESOLVE_LIMIT if limit is None else limit
        pending = pending[: max(0, budget)]
        if not pending:
            return 0

        try:
            found = await self.resolver.resolve(pending, deadline_seconds)
        except Exception as exc:  # noqa: BLE001 — funding is never worth a sweep
            log.warning("funding.batch_failed", wallets=len(pending), error=str(exc))
            return 0

        written = self.db.upsert_wallet_funding([f.as_row() for f in found])
        if written:
            self._fanout = None
            self.resolved += written
        return written

    def stats(self) -> dict[str, int]:
        return {"resolved": self.resolved, "dispensers": len(self._dispensers())}


def _exchange_label(source: FundingSource) -> str | None:
    if source.kind != KIND_EXCHANGE:
        return None
    return f"cex:{source.exchange}" if source.exchange else "cex"


def _row_to_source(row: dict[str, Any]) -> FundingSource:
    funded_at = row.get("funded_at")
    return FundingSource(
        wallet=str(row["wallet"]),
        funder=row.get("funder"),
        kind=str(row.get("kind") or KIND_UNKNOWN),
        exchange=row.get("exchange"),
        funded_at=_dt_from_block_time(funded_at),
        signature=row.get("signature"),
        source=str(row.get("source") or "solana_rpc"),
    )


__all__ = [
    "DEFAULT_FANOUT_THRESHOLD",
    "DEFAULT_RESOLVE_DEADLINE_SECONDS",
    "DEFAULT_RESOLVE_LIMIT",
    "EXCHANGE_WALLETS",
    "KIND_EXCHANGE",
    "KIND_UNKNOWN",
    "KIND_WALLET",
    "FundingIndex",
    "FundingSource",
    "FundingSourceResolver",
    "classify_funder",
    "funder_from_transaction",
]
