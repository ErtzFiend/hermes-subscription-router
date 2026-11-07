from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from agent.delegation_provider import DelegationProvider, DelegationRouteError
from agent.credential_pool import register_credential_pool_strategy, CredentialPool, PooledCredential
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

STRATEGY_ENDING_SOONEST = "ending soonest"
STRATEGY_LEAST_QUOTA = "least remaining quota"
STRATEGY_MOST_QUOTA = "most remaining quota"

WINDOW_WEEKLY = "weekly"
WINDOW_SESSION = "session"

def _load_quota_cache() -> Optional[dict[str, Any]]:
    try:
        home = os.environ.get("HERMES_HOME") or get_hermes_home()
        cache_path = Path(home) / "quota_cache.json"
        if not cache_path.is_file():
            return None
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        fetched_at_str = data.get("fetched_at")
        if not fetched_at_str:
            return None
        fetched_at = datetime.fromisoformat(fetched_at_str.replace("Z", "+00:00"))
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = (now - fetched_at).total_seconds()
        # Stale if older than 30 minutes (1800s) or clock skew in future (> -60s)
        if diff > 1800 or diff < -60:
            logger.debug("Quota cache stale or invalid (age: %.1fs)", diff)
            return None
        return data
    except Exception as exc:
        logger.debug("Failed to load quota cache: %s", exc)
        return None

def _make_strategy_selector(strategy_type: str, window_type: str):
    def selector(pool: CredentialPool, candidates: list[PooledCredential]) -> Optional[PooledCredential]:
        if not candidates:
            return None
        data = _load_quota_cache()
        if not data:
            return None
        providers = data.get("providers")
        if not isinstance(providers, dict):
            return None
        provider_data = providers.get(pool.provider)
        if not isinstance(provider_data, dict):
            for p_key, p_val in providers.items():
                if isinstance(p_val, dict) and p_key.lower() == pool.provider.lower():
                    provider_data = p_val
                    break
            if not isinstance(provider_data, dict):
                return None

        raw_accounts = provider_data.get("accounts")
        if isinstance(raw_accounts, list):
            account_records = [acc for acc in raw_accounts if isinstance(acc, dict)]
        else:
            account_records = [provider_data]

        now = datetime.now(timezone.utc)
        now_ts = now.timestamp()
        scored: list[tuple[float, int, PooledCredential]] = []

        for idx, candidate in enumerate(candidates):
            c_lbl = str(candidate.label or "").strip().lower()
            c_id = str(candidate.id or "").strip().lower()

            matched_acc = None
            for acc in account_records:
                acc_lbl = str(acc.get("label") or acc.get("account_label") or "").strip().lower()
                acc_id = str(acc.get("id") or "").strip().lower()
                # Exact identity match only. Substring or positional guessing would
                # attribute one account's quota to another, so we never guess.
                if (c_id and c_id == acc_id) or (c_lbl and c_lbl == acc_lbl):
                    matched_acc = acc
                    break

            if not matched_acc and len(account_records) == 1:
                # Provider-level quota data (one record covers every pooled account).
                matched_acc = account_records[0]

            if not matched_acc:
                continue

            # Check cooldown / locked
            cooldown_until = matched_acc.get("cooldown_until") or matched_acc.get("locked_until")
            if cooldown_until and isinstance(cooldown_until, str):
                try:
                    c_dt = datetime.fromisoformat(cooldown_until.replace("Z", "+00:00"))
                    if c_dt.tzinfo is None:
                        c_dt = c_dt.replace(tzinfo=timezone.utc)
                    if c_dt.timestamp() > now_ts:
                        continue
                except Exception:
                    pass

            windows = matched_acc.get("windows")
            if not isinstance(windows, list):
                continue

            target_window = None
            for w in windows:
                if isinstance(w, dict) and str(w.get("label", "")).strip().lower() == window_type:
                    target_window = w
                    break

            if not target_window:
                continue

            if strategy_type == "ending_soonest":
                raw_reset = target_window.get("reset_at")
                if not raw_reset or not isinstance(raw_reset, str):
                    continue
                try:
                    dt = datetime.fromisoformat(raw_reset.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    ts = dt.timestamp()
                    if ts > now_ts:
                        scored.append((ts, idx, candidate))
                except Exception:
                    pass
            elif strategy_type in ("least_quota", "most_quota"):
                remaining = target_window.get("remaining")
                if remaining is None:
                    remaining = target_window.get("available")
                if remaining is None:
                    # Real quota-cache shape: windows carry used_percent, so
                    # remaining is its complement. (A bare `percentage` field is
                    # treated as remaining for synthetic/back-compat caches.)
                    used = target_window.get("used_percent")
                    if used is not None:
                        try:
                            remaining = 100.0 - float(used)
                        except Exception:
                            remaining = None
                if remaining is None:
                    pct = target_window.get("percentage")
                    if pct is not None:
                        remaining = float(pct)
                if remaining is not None:
                    try:
                        val = float(remaining)
                        scored.append((val, idx, candidate))
                    except Exception:
                        pass

        if not scored:
            return None

        if strategy_type == "ending_soonest":
            scored.sort(key=lambda item: (item[0], item[1]))
        elif strategy_type == "least_quota":
            scored.sort(key=lambda item: (item[0], item[1]))
        elif strategy_type == "most_quota":
            scored.sort(key=lambda item: (-item[0], item[1]))

        return scored[0][2]
    return selector

# Register strategies for both window types and variants
for w in (WINDOW_WEEKLY, WINDOW_SESSION):
    register_credential_pool_strategy(f"ending_soonest:{w}", _make_strategy_selector("ending_soonest", w))
    register_credential_pool_strategy(f"least_remaining_quota:{w}", _make_strategy_selector("least_quota", w))
    register_credential_pool_strategy(f"most_remaining_quota:{w}", _make_strategy_selector("most_quota", w))

# Default aliases
register_credential_pool_strategy("ending_soonest", _make_strategy_selector("ending_soonest", WINDOW_WEEKLY))
register_credential_pool_strategy("least remaining quota", _make_strategy_selector("least_quota", WINDOW_WEEKLY))
register_credential_pool_strategy("most remaining quota", _make_strategy_selector("most_quota", WINDOW_WEEKLY))


class SubscriptionRouterBackend(DelegationProvider):
    """Quota-aware subscription routing backend supporting Ending Soonest,

    Least remaining quota, and Most Remaining Quota strategies across weekly
    and session quota windows.
    """

    @property
    def name(self) -> str:
        return "subscription-router"

    @property
    def display_name(self) -> str:
        return "Subscription Router"

    @property
    def summary(self) -> str:
        return (
            "Route tasks based on quota windows and account selection strategy "
            "(Ending Soonest, Least remaining quota, Most Remaining Quota)."
        )

    def task_parameters(self, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        # Per-task strategy/window overrides are intentionally NOT offered: the
        # credential pool selects accounts per provider from config, so a per-task
        # value could only relabel the route without steering selection. The active
        # controls are the provider-level config keys documented in README.md.
        return {}

    def description(self, config: Mapping[str, Any]) -> str:
        return (
            "Quota-aware subscription account selection. Configure per provider with "
            "credential_pool.strategies.<provider>: 'ending_soonest:weekly' (default), "
            "'ending_soonest:session', 'least_remaining_quota:weekly', "
            "'least_remaining_quota:session', 'most_remaining_quota:weekly', or "
            "'most_remaining_quota:session'."
        )

    def is_available(self) -> bool:
        return True

    def route_task(self, task: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        settings = self.settings(config)
        strategy = str(settings.get("strategy") or "Ending Soonest").strip()
        window = str(settings.get("window") or WINDOW_WEEKLY).strip().lower()
        if window not in (WINDOW_WEEKLY, WINDOW_SESSION):
            raise DelegationRouteError(f"subscription-router: invalid window {window!r}; use 'weekly' or 'session'")

        base_route = dict(config)
        existing_label = base_route.get("label")
        label_suffix = f"{strategy} ({window})"
        final_label = f"{existing_label} | {label_suffix}" if existing_label else label_suffix

        enriched = {
            **base_route,
            "label": final_label,
        }
        return enriched


def register(ctx) -> None:
    ctx.register_delegation_provider(SubscriptionRouterBackend())
