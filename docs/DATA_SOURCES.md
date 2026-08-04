# Data sources

Every endpoint below was called live and its response inspected on **2026-07-25**.
Field names are transcribed from actual response bodies, and rate limits come
from response headers or published documentation rather than from folklore.

This matters more than usual in this domain. Most published guides to the
pump.fun API describe endpoints that now return 404: the surface was sharded
across four hosts, and `frontend-api-v3` — which almost every tutorial still
names as the single source — is now a metadata service only. Trades, candles and
market activity moved to `swap-api`; holder forensics moved to `advanced-api-v2`.

**Treat this file as perishable.** These are undocumented internal APIs on
products that reshape themselves without notice. When a collector starts
returning nothing, come here first, re-verify, and update both the doc and the
code together.

---

## Rate limits — the binding constraint

| Host | Limit | Source | How Botsensai spends it |
|---|---|---|---|
| `swap-api.pump.fun` | 1000 / 60s | `x-ratelimit-limit` header | Freely. Trades and market activity for every candidate. |
| `frontend-api-v3.pump.fun` | 50 / 60s | `x-ratelimit-limit` header | Discovery listing, SOL price, currently-live. |
| `advanced-api-v2.pump.fun` | 60 / 60s | `x-ratelimit-limit` header | **Scarcest budget in the system.** Holder forensics for ~6 tokens per sweep, top-ranked only. |
| `api.dexscreener.com` pairs/search | 300 / min | documented | Batched 30 addresses per call. |
| `api.dexscreener.com` boosts/profiles/orders/metas | 60 / min | documented | Separate pacer. Order ledger for ~20 tokens per sweep. |
| `api.geckoterminal.com` | 30 / min **shared across all endpoints** | documented | New pools, trending, and safety screening for ~8 tokens per sweep. |
| `livestream-api.pump.fun` | undocumented | — | Self-throttled to 30/min. |

Each host gets its own `PacedClient` with its own token bucket and circuit
breaker, so a 429 storm on one cannot throttle the others. The reason the
pipeline has a free local screening step between discovery and enrichment is
entirely this table: enriching everything would exhaust the 60/min budgets in
seconds.

---

## pump.fun

Sharded across four hosts. No auth, no API key, no cookie on any read endpoint.
All hosts sit behind Cloudflare (`server: cloudflare`, `cf-ray` present) but are
not currently bot-walled — a bare request from a datacenter IP with no
User-Agent returns 200.

### frontend-api-v3.pump.fun — metadata

```
GET /coins?limit=1..100&offset=N&sort=created_timestamp&order=DESC&includeNsfw=false
GET /coins/{mint}
GET /coins/currently-live?limit=N&offset=N
GET /sol-price
GET /global-params/{unix_seconds}
```

Coin objects carry ~50 fields. The ones Botsensai reads: `mint`, `name`,
`symbol`, `description`, `image_uri`, `metadata_uri`, `twitter`, `telegram`,
`website`, `creator`, `created_timestamp` (epoch **milliseconds**), `complete`,
`virtual_sol_reserves`, `virtual_token_reserves`, `real_sol_reserves` (all in
lamports / micro-tokens), `market_cap` (SOL), `usd_market_cap`, `total_supply`,
`raydium_pool`, `pump_swap_pool`.

`/sol-price` returns `{solPrice, asOfTimestamp, stale}`. It is **not** optional:
`market_cap` is denominated in SOL, and mixing it with USD liquidity from
Dexscreener without converting produces silently wrong metrics.

`/coins/currently-live` adds livestream fields including `num_participants`, a
real-time viewer count. Concurrent viewers are hard to fake at the scale these
launches operate at, which makes it one of the better attention signals available.

### swap-api.pump.fun — the tape

```
GET /v2/coins/{mint}/trades?limit=1..100&cursor={nextCursor}
GET /v1/coins/{mint}/market-activity
GET /v1/coins/{mint}/candles?interval=1m|5m|1h&limit=N
GET /v1/coins/{mint}/ath
POST /v1/coins/ath/batch  {"addresses": [...]}
```

`limit` above 100 returns **HTTP 400**, and a page returns roughly 20 rows
regardless, so depth comes from following `pagination.nextCursor` while
`pagination.hasMore` is true. Botsensai follows six pages, because the sniper and
bundle metrics need a token's *first* trades, not its most recent twenty.

Trade row fields, verified: `tx` (signature — **not** `signature`), `timestamp`
(ISO-8601 string — **not** epoch millis), `userAddress`, `type` (`buy`/`sell`),
`amountSol`, `baseAmount`, `quoteAmount`, `priceSol`, `priceUsd`, `program`,
`slotIndexId`.

`slotIndexId` looks like `0004351948800012900000`: a zero-padded 12-digit slot
followed by a 10-digit intra-slot index. Botsensai decodes the slot from it,
because slot ordering is what the sniper and bundle-detection metrics key on and
it is not exposed anywhere else on this endpoint.

`/market-activity` is the highest-value single call: an object keyed by interval
(`5m`, `1h`, `6h`, `24h`), each carrying transaction counts, volume, and unique
participant counts.

### advanced-api-v2.pump.fun — forensics

```
GET /coins/top-holders/{mint}
```

Returns `topHolders[]` with `address`, `amount`, and pre-computed `isDev`,
`isSniper`, `isBundler` flags.

Botsensai records these flags **as labels only** and does not depend on them. The
topology metrics reconstruct the same conclusions from raw trade data, so the
signal survives this endpoint being withdrawn, rate-limited into uselessness, or
changing its labelling methodology without notice. Depending on a vendor's
proprietary labels for your core signal means your edge is on loan.

### Streams

- `wss://pumpportal.fun/api/data` — send `{"method":"subscribeNewToken"}` or
  `{"method":"subscribeMigration"}`. No auth for those two; an API key is
  required for per-token and per-account trade streams. Each subscription is
  acknowledged with a `{"message": "..."}` frame before any data arrives.

  Full create-event shape, captured live 2026-07-30 — the last eight fields are
  not in pumpportal's own documentation:

  ```json
  {"signature": "4Lhz…", "mint": "97QU…pump", "traderPublicKey": "HpAR…",
   "txType": "create", "initialBuy": 27534883.660147, "solAmount": 0.790123455,
   "bondingCurveKey": "Cm7t…", "vTokensInBondingCurve": 1045465116.339853,
   "vSolInBondingCurve": 30.790123454, "marketCapSol": 29.451124646,
   "name": "Smooth Like Butter", "symbol": "SLB", "uri": "https://ipfs.io/…",
   "is_mayhem_mode": false, "pool": "pump"}
  ```

  **There is no timestamp in the frame.** No `created_timestamp`, no
  `blockTime`, nothing. A stream-discovered launch can therefore only record its
  receipt time as `created_at`, which is an upper bound on the mint time;
  `frontend-api-v3/coins` remains the only source of the authoritative
  `created_timestamp`. `Database.upsert_launch` keeps the minimum of the two so
  the approximation is corrected as soon as a sweep corroborates it, and
  `Database.observation_latency()` reports corroborated rows separately from the
  total so an uncorroborated zero is never averaged in as a measurement.

  `solAmount` on a create event is the deployer buying their own token in the
  mint transaction — this is the cheapest source of `dev_buy_sol` in the system,
  and the REST listing does not carry it at all. `vTokensInBondingCurve` is a
  *virtual reserve* and is not a supply figure; do not populate
  `initial_supply` from it. Every number in the frame is denominated in SOL.

  Measured value, 2026-07-30, over 57 mints taken off the socket and then
  corroborated by one REST sweep: **median 1.31s from mint to first observation,
  p90 1.73s**, against **88.6s median** for the same store's 887 poller-first
  launches. All 57 were novel — the socket beat the poller every time.
  Rate: roughly 30 create events per minute. Read by `botsensai stream`.
- `wss://livechat.pump.fun/socket.io/?EIO=4&transport=websocket` — Socket.IO v4.
  **This is the reply/comment system**; the old REST `/replies/{mint}` is gone.
  Aggressive per-IP connection throttling was observed: a second connection
  within ~60s was refused at handshake. Reuse one socket and multiplex rooms.

---

## Dexscreener

Treated primarily as a **paid-promotion ledger** rather than a market-data
source. Price and volume are available in several places; who paid for
visibility, and how much, is published nowhere else.

```
GET /token-profiles/latest/v1                    60/min
GET /token-boosts/latest/v1                      60/min
GET /token-boosts/top/v1                         60/min
GET /orders/v1/{chainId}/{tokenAddress}          60/min
GET /ads/latest/v1                               60/min
GET /community-takeovers/latest/v1               60/min
GET /metas/trending/v1                           60/min
GET /latest/dex/pairs/{chainId}/{pairId}        300/min
GET /tokens/v1/{chainId}/{addr,addr,...}        300/min   (up to 30 addresses)
GET /token-pairs/v1/{chainId}/{tokenAddress}    300/min
GET /latest/dex/search?q={query}                300/min   (hard cap: 30 results)
```

`/orders/v1/...` is the promotion ledger for a single token: profile purchases,
trending-bar ads and community takeovers, with status. Marketing spend is
informative in both directions — a token spending heavily on boosts while its
organic engagement stays flat is buying the appearance of traction, and the
`boost_to_liquidity` ratio makes that comparison directly.

`/metas/trending/v1` is Dexscreener's own read on which narratives are hot. It
feeds the `meta_alignment` metric. A live call on 2026-07-25 returned: Cat,
Internet Animals, Dog, Meme Hall of Fame, AI, Character.

`wss://api.dexscreener.com/token-boosts/latest/v1` connects and streams without
auth (send a browser `Origin`). The full frontend screener stream at
`wss://io.dexscreener.com/dex/screener/v5/...` returns **HTTP 403** to non-browser
TLS fingerprints — reachable only through the browser driver, if at all.

---

## GeckoTerminal

```
GET /api/v2/networks/solana/new_pools?page=1
GET /api/v2/networks/solana/trending_pools?duration=5m&page=1
GET /api/v2/networks/solana/tokens/multi/{addr,addr,...}
GET /api/v2/networks/solana/tokens/{address}/info
GET /api/v2/networks/solana/pools/{pool}/ohlcv/{timeframe}?aggregate=N&limit=N
```

30 calls/min **shared across every endpoint**, so this is spent carefully.

Two things make it the most valuable free surface in the stack. `new_pools`
gives an exact `pool_created_at` timestamp, and precise pool age is the single
most important field for a strategy whose decision window is minutes. And
`/tokens/{address}/info` carries token-level safety fields — `mint_authority`,
`freeze_authority`, `is_honeypot`, `developer_holding_percentage`,
`top_10_holder_percentage`, plus a composite `gt_score` with per-category detail
— which feed the veto gates directly. That is the highest-leverage possible use
of a scarce budget: one call can eliminate a token entirely and save every other
collector the work.

Send `Accept: application/json;version=20230302` to pin the response schema.

Percentages arrive as 0–100 and are converted to 0–1 on ingest.

---

## Other launchpads

The picture here has changed substantially and several widely-repeated premises
are now stale.

**Moonshot is now Moonit.** The domain is `moon.it`; the Dexscreener `dexId`
changed from `moonshot` to `moonit`. `api.moonshot.cc` and `api.moon.it` are
NXDOMAIN. The live public API is `https://api.mintlp.io/v1/fun?sortBy=...&state=...`,
open with no auth. Invalid query parameters return a structured HTTP 400 whose
message array leaks the full enum, which is how the sort and state values were
enumerated. The venue is close to dormant: 80 non-graduated tokens total with
the newest launch on 2026-07-22, and only five graduations since April 2026.
Note that `moonshot.money` is a separate Dexscreener mobile/fiat-onramp product,
not this launchpad.

**Ape.store is no longer on Blast or Degen.** `GET /api/config` returns exactly
four chains: Base (8453, active), Ethereum (1, inactive), BNB (56, inactive) and
RobinHood Chain (4663, active). Robinhood Chain is where the July-2026 launch
activity actually is. `GET /api/tokens?chain={numericChainId}&sort={0..5}` returns
24 items per page; fields include `isKing`, `kingDate`, `dexPaid`, `isStreaming`,
`streamViewers`, `chatCount`. Note `/api/config` returns JavaScript, not JSON.

**Believe is near-dead as a launch venue.** DefiLlama shows 24h fees of $0 and
30d fees of $122, against $36.71M in Q2 2025. `api.believe.app` is a live but
closed Express server, and the frontend sits behind a Vercel security checkpoint
that returns HTTP 429 to non-browser clients. The Believe authority key is still
active on-chain, but its recent successful transactions are `ClaimPositionFee`
against Meteora DAMM v2 — harvesting legacy LP fees, not launching.

Practical reading: treat Moonit and Believe as historical corpora for backtests
and as low-frequency long-tail venues; Ape.store on Robinhood Chain is the only
one of the three with live new-launch flow.

---

## Social

### X — two live paths, and three dead ones

**The dead ones matter most.** These return **HTTP 200 with a zero-byte body**
and no content type. Not 404, not 429 — a success status with nothing in it.
Almost every published scraper and blog post still calls them:

```
cdn.syndication.twimg.com/timeline/profile     DEAD — 200, 0 bytes
cdn.syndication.twimg.com/timeline/tweet       DEAD — 200, 0 bytes
cdn.syndication.twimg.com/widgets/timelines    DEAD — 200, 0 bytes
```

A collector that trusts the status code records "no social activity" for every
token forever and never raises an alarm. Downstream, that reads as a bearish
signal about the entire market. `collectors/social.py` therefore treats an empty
success as an error via `_require_body`, and `tests/test_collectors.py` has a
regression guard that fails the build if any of these three strings reappear.

The two paths that do work sit on **different hosts with opposite rate limits**,
and that asymmetry drives the whole design:

```
GET https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}
    → HTML; payload in <script id="__NEXT_DATA__">
    → MEASURED: 12 requests, then hard 429. Still 429 at t+311s. Recovers ~t+600s.
    → i.e. ~12 per 15 minutes — under one request per minute.

GET https://cdn.syndication.twimg.com/tweet-result?id={id}&token={token}&lang=en
    → JSON, single tweet
    → MEASURED: 40/40 consecutive requests returned 200, no throttling observed.
```

`token` is derived client-side from the tweet id:
`((id / 1e15) * Math.PI).toString(36).replace(/(0+|\.)/g, '')`. Reimplemented as
`x_tweet_token()`; without it the endpoint refuses the request.

The consequence: the profile path is the **only free source of follower counts
and account creation dates**, and it gets roughly twelve calls per quarter hour,
so it is spent only on a token's own named account and cached for an hour. The
tweet path returns only `favorite_count` and `conversation_count` (verified by a
full key dump — there is no retweet, quote or reply count) but is effectively
unthrottled, which makes it an engagement-*velocity* probe rather than a
snapshot. Polling it every fifteen seconds is what feeds the cadence analysis in
`reply_rhythm_naturalness`.

Two field-level traps, both verified:

- **`user.id` is always `0`.** The real identifier is `user.id_str`. Anything
  keying on `user.id` collapses every account into a single bucket, destroying
  every per-account metric without producing one error.
- **`fast_followers_count` and `normal_followers_count` exist** in this payload.
  `fast_followers` is X's own count of followers acquired in bursts — which is
  exactly what a purchased package produces. It is about as close to a direct
  bought-follower oracle as any public source offers, and it is far better
  evidence than inferring the same thing from an engagement ratio.

Also verified: some accounts return `{"entries": []}` with HTTP 200. That is
genuine absence for that handle, not an error, and is treated as such. The
official X API v2 went pay-per-use in February 2026 with no free tier at
$0.005 per post read, so it is not a viable primary feed. Nitter is gone — it
depended on the guest-token endpoint X disabled. X Communities were deleted on
2026-05-30; do not build on them.

### Reddit — the mirror, not the source

`reddit.com/r/{sub}/new.json` returns **HTTP 403 with a ~190 KB HTML
interstitial** to datacenter IPs. It is not throttled, it is refused.

```
GET https://arctic-shift.photon-reddit.com/api/posts/search?subreddit=&query=&limit=&sort=desc
```

Arctic Shift mirrors the full 108-field Reddit post schema, is unauthenticated,
and answers from the same addresses Reddit refuses. Switching to it took this
collector from permanently `down` in `botsensai doctor` to reachable.

Tickers shorter than three characters are skipped entirely — free-text search on
them produces an overwhelming false-positive rate, and not collecting is more
honest than filtering afterwards.

### 4chan /biz/ — unfashionable, genuinely useful

```
GET https://a.4cdn.org/biz/catalog.json       → 11 pages, ~200 threads
GET https://a.4cdn.org/biz/threads.json       → 3 fields/thread; poll this
GET https://a.4cdn.org/biz/thread/{no}.json   → full thread
```

One documented rule: no more than one request per second, and use
`If-Modified-Since`. Tickers surface here before they surface anywhere with a
moderation team. Every image post carries a native **`md5`**, which makes
exact cross-surface image identity free rather than requiring a perceptual hash.

### Telegram — the web preview

```
GET https://t.me/s/{channel}    → last 20 messages as plain HTML, no auth
```

Selectors: `div.tgme_widget_message[data-post]`, `time[datetime]`,
`span.tgme_widget_message_views`. View counts render abbreviated (`3.46M`,
`1.44K`) and must be expanded.

This is the only workable path. The Bot API is structurally useless — a bot
receives channel posts only if it is an administrator, which no call channel
will grant — and MTProto requires binding a real phone number to an account that
will be banned for precisely this behaviour.

Call channels front-run retail by minutes, so reading them at the moment they
post is one of the few genuinely timing-sensitive edges available.

### One trap shared by 4chan and Telegram

Both encode `$` as `&#036;`. Strip tags without decoding entities and the
cashtag regex matches nothing at all — every mention-based metric silently reads
zero on both surfaces while appearing to work.

### TikTok, Instagram, YouTube

`https://www.tiktok.com/oembed?url=...` works unauthenticated and returns the
full caption including hashtags. Everything richer needs either the Research API
(application and research proposal required) or a signed web request. Instagram
and YouTube are queued in `@fix_plan.md` phase 2. All go through the browser
driver against public pages only.

## On-chain

Solana JSON-RPC works for verification but the public endpoint rate-limits at
roughly 10 req/s and 429s frequently. Any polling loop needs a paid provider —
Helius, Triton or QuickNode. Yellowstone gRPC / Geyser (or Helius LaserStream)
is the right answer for sub-slot streaming if latency becomes the binding
constraint; `transaction.index` within a slot is the field that makes bundle
contiguity and sniper ordering computable.

**Measured 2026-08-04** against `api.mainnet-beta.solana.com`, resolving funding
sources for 162 wallets: **234 calls in 241.9 s at 120 req/min (2/s) with no
429**, i.e. one fifth of the published ceiling holds comfortably. That is the
figure `Settings` clamps `collectors.solana_rpc` to; it is not a licence to
widen it, because the same endpoint serves the sweep.

Two calls answer a wallet: `getSignaturesForAddress` for the oldest signature,
then `getTransaction`. **There is no cheap query for "oldest transaction" on a
busy address** — `getSignaturesForAddress` pages newest-first, so a wallet with
more than one 1000-signature page costs a call per page to walk. `onchain/funding.py`
stops rather than paying that, and records the wallet as *unresolved*, never as
unfunded. On the current corpus that is **101 of 175 holder wallets (58%)**: the
resolvable population is skewed toward fresh wallets, which is the population
these metrics care about, but it is a real coverage ceiling and a paid provider
with an enhanced-history endpoint is what lifts it.

Four correctness traps that silently corrupt holder analysis:

- **`getTokenAccounts` returns token accounts, not owners.** One wallet holds
  many ATAs of the same mint. Aggregating rows instead of grouping by `owner`
  inflates holder count and deflates every concentration measure.
- **Program-owned reserves must be excluded before computing concentration.**
  Pre-graduation the bonding-curve ATA holds most of the supply, so a naive
  top-10 share reads near 100% for every healthy token on the platform. Pool
  vaults and the burn address `1nc1nerator11111111111111111111111111111111`
  cause the same distortion afterwards. `metrics.topology.tradeable_holders`
  strips these and renormalizes, so a "40% of supply" figure means 40% of what
  can actually be sold.
- **pump.fun `create` and `buy` can occur in the same transaction**, so a dev
  pre-buy is invisible if you only diff across transactions. Read
  `preTokenBalances` / `postTokenBalances` of the create transaction itself.
- **LaserStream truncates `logMessages` at 10 KB** and silently ignores every
  subscription filter if `ping` is present in the initial `SubscribeRequest`.

### The base rate is not stationary, and this is quantified

pump.fun graduation ran under 2% in Q4 2024 and had fallen to roughly **0.63%**
by late 2025. Any threshold calibrated against the old number classifies every
subsequent day as "dead" and silently switches the strategy off. This is why
`ScoringSettings.regime_hot_graduation_rate` and
`regime_dead_graduation_rate` are configuration with a recorded
`regime_calibrated_on` anchor rather than constants.

Related traps from the empirical literature, all of which the backtester is
built to avoid: survivorship bias in token datasets is enormous and measured;
the universe must be enumerated **from mints, not from a price API**, because
any "top tokens" feed has already conditioned on survival; socials are routinely
added *after* launch, so social-presence effects are only real if read from
creation-time metadata; and accuracy and ROC-AUC are meaningless at a 0.6% base
rate, which is why this system reports top-decile lift and bootstrap intervals
instead.

---

## What none of these publish

Worth stating plainly, because it is the reason the metric suite exists. None of
the sources above will tell you whether a token's 400 replies came from 400
people or from one person with 400 accounts; whether its holders were funded
from one wallet last Tuesday; whether anyone unconnected to the team has made a
single original meme about it; or how much of the position you are contemplating
could actually be sold.

Those are the questions the 32 metrics answer, and they are answered by
recombining these feeds rather than by finding a feed that reports them.
