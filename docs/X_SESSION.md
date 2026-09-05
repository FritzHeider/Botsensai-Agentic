# Session-backed X collection

## Why bother

Three inputs carry most of the weight in the social-authenticity family, and none
of them exist on any free unauthenticated path:

| input | what needs it | free path? |
|---|---|---|
| reply **text** | `reply_template_ratio`, `conviction_language_share` | no — syndication carries counts only |
| `bookmark_count`, views | `engagement_depth_ratio` | no — verified absent from every free payload |
| per-engager account creation dates | `engager_age_dispersion` | no — needs Favoriters/Retweeters |
| ticker search | `mention_author_diversity`, `social_velocity_acceleration` | no — no free search exists |
| `fast_followers_count` | `purchased_follower_signal` | profile payload only |

That is roughly 18% of the composite score currently running near zero coverage
on X. `reply_template_ratio` in particular is the highest-weighted social metric
and the cheapest bot detector in the suite, and without reply bodies it cannot
run at all.

There is also a rate-limit argument. The unauthenticated profile endpoint allows
about **twelve requests per fifteen minutes per IP** — measured, and it stays
429 for roughly ten minutes after tripping. Authenticated GraphQL limits are
per-account fifteen-minute windows that are more generous by orders of magnitude.

## What this costs you

Be clear-eyed about it:

- **Automated reads from a logged-in account are against X's terms.** The
  realistic enforcement is suspension of that account, not anything worse — but
  it is a likely eventual outcome, not a hypothetical one.
- **Use a throwaway account.** Not your main, not one tied to anything you care
  about, not one with your real phone number on it if you can avoid that.
- **Rate limits still bite.** Hammering GraphQL from one account is the fastest
  route to being flagged. The defaults here are set to what a person reading
  actively would generate — around twenty requests a minute — and you should not
  raise them.
- **Coverage becomes dependent on a fragile thing.** If the account is suspended,
  social coverage drops back to where it is now. The metric layer handles that by
  lowering confidence rather than by reporting bad news, and `botsensai doctor`
  will show it, but it does mean this is not a permanent fix.

Botsensai never sees a credential. There is no password field, no login flow
anywhere in the codebase, and no cookie is ever written into the repository. You
log in by hand, once; the collector reads the browser profile you logged into.7

## Setup — the short version

Make a throwaway X account first, in a normal browser window. Then, on the
machine that will do the collecting:

```bash
botsensai x-setup
```

That creates the profile directory, launches Chrome against it, waits while you
log in, verifies the session actually took, and writes the three config values.
It asks you to type `burner` to confirm you accept the account may be
suspended — that acknowledgement is a decision, so it is not a default-yes.

It refuses to put the profile inside a git repository (a profile holds live
session cookies, and inside a repo it is one `git add -A` from being published),
it detects the case where Chrome is still holding the profile lock — the most
common setup failure, and one that otherwise surfaces as an opaque Playwright
error — and it backs the config up before writing.

If anything goes wrong it writes nothing and tells you what to fix. The manual
path below still works and is worth reading once so you know what the command is
doing on your behalf.

## Setup — the manual version

**1. Make a throwaway X account.** Do this in a normal browser window. Nothing
about this step involves Botsensai.

**2. Create a dedicated Chrome profile directory and log in to it by hand.**

On macOS:

```bash
mkdir -p ~/botsensai-x-profile

# Launch Chrome pointed at that directory, then log in to X in the window
# that opens. Use the throwaway account.
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --user-data-dir="$HOME/botsensai-x-profile" \
  https://x.com/login
```

On Linux, substitute `google-chrome` or `chromium`.

Log in, confirm you can see your timeline, then **close the window completely**.
Chrome holds a lock on the profile directory while it is running, and Playwright
cannot open a locked profile.

Keep the directory **outside the repository**. `.gitignore` covers the obvious
cases, but the right answer is for it to live in your home directory where it
cannot be committed by accident.

**3. Point Botsensai at it and check.**

```yaml
# config/botsensai.yaml
browser:
  user_data_dir: /Users/you/botsensai-x-profile

x_session:
  enabled: true
  acknowledged_burner: true    # only after you have confirmed which account this is
  requests_per_minute: 20
  max_tokens_per_sweep: 6
```

Then verify:

```bash
botsensai x-session
```

This loads `x.com/home` in that profile and looks for a marker that only renders
for a logged-in viewer. It prints one of three things: the session is active and
which handle it belongs to, the session is not authenticated and why, or the gate
is still closed and which requirement is missing.

**Run this before trusting any social coverage figure**, and again whenever
coverage drops. An expired session produces empty results that look exactly like
tokens with no social activity — the worst failure mode in the collection layer,
and the reason this command exists rather than the state being inferred.

## The three-part gate

Session collection needs all of these to be true, and they are deliberately
separate:

```
x_session.enabled              — the feature is on
x_session.acknowledged_burner  — you have thought about which account this is
browser.user_data_dir          — a profile is configured
+ the profile actually verifies as logged in, checked at runtime
```

`acknowledged_burner` exists because "I turned a feature on" and "I have
considered that this account may be suspended" are different decisions, and
collapsing them into one flag makes the second one easy to skip.

## What it collects, and the budget

Per token, in order, stopping early when a threshold is not met:

1. **One** search page load for `$TICKER`, scrolled a few times. Yields posts
   with views, bookmarks, follower counts and author creation dates.
2. If the most-engaged post has at least `reply_threshold` replies (default 5),
   **one** thread page load for reply bodies. This is the unlock for
   `reply_template_ratio`.
3. If that post has at least `engager_threshold` total engagement (default 25),
   **two** page loads for the likers and reposters lists, for engager account
   ages.

So a token costs between one and four page loads, and only the single
highest-engagement post gets deepened. Pulling replies for every post in a search
result would be dozens of loads per token for very little extra signal and would
get the account flagged quickly.

The browser runs **headed**, not headless: X serves a materially different and
more challenge-prone experience to headless clients even with a valid session.
That means the machine running collection needs a display, which in practice
means running it on your own computer rather than in a cloud container.

## Where it runs

The profile lives on your machine. A cloud container's browser is a different
browser on a different host and cannot see it. So session collection is a
**local** job:

```bash
# On the machine with the profile
botsensai x-session          # verify first
botsensai sweep --repeat 1   # one pass with session collection active
```

The rest of the system — the unauthenticated collectors, the scorer, the
backtester — runs anywhere.

## If it stops working

Re-running `botsensai x-setup` is usually the fastest fix — it will re-launch
Chrome against the existing profile so you can log in again.

`botsensai x-session` tells you which of these it is:

- **"no browser profile configured"** — `browser.user_data_dir` is unset.
- **"profile directory does not exist"** — path typo, or the directory was never
  created.
- **"login wall present"** — the session expired or the account was suspended.
  Re-launch Chrome against the profile, log in again, close it.
- **"browser unavailable"** — Playwright is not installed. `pip install -e
  ".[browser]" && playwright install chromium`.
- **"no authentication marker found"** — ambiguous page. Treated as logged out on
  purpose: a false negative costs one collector, a false positive corrupts every
  social metric.

If the account is gone, the collector falls back to the public path
automatically and marks the result degraded. Nothing breaks; coverage just
returns to where it was.
