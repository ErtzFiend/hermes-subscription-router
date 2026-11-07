# subscription-router

Quota-aware **credential selection** for Hermes Agent credential pools. Choose which pooled
subscription account gets used with one of three deterministic strategies, each over a weekly or
session quota window:

| Strategy key | Behavior |
|---|---|
| `ending_soonest` *(default)* | Pick the account whose selected window resets soonest. |
| `least_remaining_quota` | Pick the account with the least quota left in the window (burn down near-empty accounts first). |
| `most_remaining_quota` | Pick the account with the most quota left (spread load onto the fullest account). |

Window suffix: `:weekly` or `:session` (e.g. `least_remaining_quota:session`). No suffix = weekly.

## How it works

The plugin registers six selection strategies into a small, generic core seam
(`register_credential_pool_strategy()` in `agent/credential_pool.py`). The Hermes credential pool
then consults the plugin when it selects an account for the configured provider. The plugin reads
`$HERMES_HOME/quota_cache.json` (the same cache the desktop Quota widget uses) and scores only
accounts it can identify **by exact id or label match** — it never guesses by position or name
similarity, never reads or copies API keys from the cache, and never touches secrets.

Safety semantics:

- Cache must exist and be fresh (≤ 30 min old; future-skewed caches rejected). Otherwise the
  plugin abstains and the pool falls back to its default order (`candidates[0]`).
- Accounts in cooldown / lockout (`cooldown_until` / `locked_until` in the future) are skipped.
- Accounts with no matching quota record, or no data for the requested window, are skipped.
- Ties break deterministically by account order in the pool.
- Zero-remaining accounts are treated as legitimately exhausted: under `most_remaining_quota`
  they simply score lowest and lose; under the other strategies they win only if their reset is
  genuinely soonest — the pool's own exhausted-credential cooldown still applies upstream.

## Requirements

- Hermes with the **credential-pool strategy seam**. Apply
  [`patches/router-core-seam.patch`](patches/router-core-seam.patch) (a ~50-line generic registry hook in
  `agent/credential_pool.py`) to your Hermes checkout, or use a Hermes release that includes it.
  The plugin is honest about this: without the seam it cannot influence account selection, and
  `run_tests.sh` refuses to run against unpatched core.
- `~/.hermes/quota_cache.json` with provider quota data (produced by the Quota plugin's fetchers).

## Install

```bash
# from the release archive
mkdir -p ~/.hermes/plugins/subscription-router
tar -xzf subscription-router-1.0.0.tar.gz -C ~/.hermes/plugins/
```

## Configure

`config.yaml` (top level, per provider):

```yaml
credential_pool_strategies:
  openrouter: most_remaining_quota:weekly   # or any of the six keys below
```

Valid keys: `ending_soonest:weekly` *(default)*, `ending_soonest:session`,
`least_remaining_quota:weekly`, `least_remaining_quota:session`,
`most_remaining_quota:weekly`, `most_remaining_quota:session`.

Optionally register the delegation provider so routes are labelled with the active
strategy/window:

```yaml
plugins:
  enabled:
    - subscription-router
delegation:
  backends:
    subscription-router:
      strategy: "Most Remaining Quota"   # observability/label only
      window: weekly
```

Per-task strategy/window overrides are deliberately **not** supported: the credential pool
selects accounts per provider from config, so a per-task value could only relabel the route.
Selecting a strategy is a one-line config change.

## Test

```bash
HERMES_SRC=/path/to/patched/hermes-agent ./run_tests.sh
```

Runs the plugin's integration tests **and** the core credential-pool regression tests against a
disposable `HERMES_HOME`. The six strategy/window combinations are exercised through the real
`CredentialPool._select_unlocked()` path, proving each strategy changes the selected account and
that the default behaviour is preserved when no quota data is available.

## Rollback

Remove the `credential_pool_strategies` entry (or change the key back to a built-in strategy such
as `earliest_weekly_reset`) and remove the plugin directory. The seam itself is inert when no
plugin strategies are registered.

## License

MIT — Copyright (c) 2026 Jacob Hodge.
