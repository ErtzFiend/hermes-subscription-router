from __future__ import annotations

import json
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import pytest

from agent.credential_pool import CredentialPool, PooledCredential

# Load subscription-router plugin dynamically by file path to avoid hyphen import issues
PLUGIN_DIR = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("subscription_router", PLUGIN_DIR / "__init__.py")
subscription_router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subscription_router)

SubscriptionRouterBackend = subscription_router.SubscriptionRouterBackend


def make_cred(id_str, label=None, priority=0):
    return PooledCredential(
        id=id_str,
        provider="test-provider",
        auth_type="oauth",
        priority=priority,
        label=label or "",
        source="oauth",
        access_token="secret-token",
    )


def test_plugin_metadata():
    backend = SubscriptionRouterBackend()
    assert backend.name == "subscription-router"
    assert backend.display_name == "Subscription Router"
    assert backend.is_available() is True

    # Per-task strategy/window overrides are deliberately not offered: account
    # selection is config-driven (credential_pool.strategies.<provider>), so a
    # per-task value could only relabel the route.
    assert backend.task_parameters({}) == {}


@pytest.fixture
def quota_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    now = datetime.now(timezone.utc)
    cache_file = tmp_path / "quota_cache.json"

    reset_a = (now + timedelta(days=1)).isoformat()
    reset_b = (now + timedelta(days=3)).isoformat()
    session_reset_a = (now + timedelta(hours=1)).isoformat()
    session_reset_b = (now + timedelta(hours=5)).isoformat()

    cache_data = {
        "fetched_at": now.isoformat(),
        "providers": {
            "test-provider": {
                "accounts": [
                    {
                        "label": "Account-A",
                        "windows": [
                            {"label": "weekly", "reset_at": reset_a, "remaining": 10.0},
                            {"label": "session", "reset_at": session_reset_a, "remaining": 5.0},
                        ],
                    },
                    {
                        "label": "Account-B",
                        "windows": [
                            {"label": "weekly", "reset_at": reset_b, "remaining": 90.0},
                            {"label": "session", "reset_at": session_reset_b, "remaining": 50.0},
                        ],
                    },
                ]
            }
        },
    }
    cache_file.write_text(json.dumps(cache_data))
    return tmp_path


def test_six_credential_pool_combinations(quota_setup):
    c1 = make_cred("id-a", label="Account-A", priority=0)
    c2 = make_cred("id-b", label="Account-B", priority=1)

    pool = CredentialPool(
        provider="test-provider",
        entries=[c1, c2],
    )

    # 1. Ending Soonest (Weekly) -> Account-A
    pool._strategy = "ending_soonest:weekly"
    chosen, _ = pool._select_unlocked(refresh=False, count=False)
    assert chosen.id == "id-a"

    # 2. Ending Soonest (Session) -> Account-A
    pool._strategy = "ending_soonest:session"
    chosen, _ = pool._select_unlocked(refresh=False, count=False)
    assert chosen.id == "id-a"

    # 3. Least Remaining Quota (Weekly) -> Account-A (10.0 vs 90.0)
    pool._strategy = "least_remaining_quota:weekly"
    chosen, _ = pool._select_unlocked(refresh=False, count=False)
    assert chosen.id == "id-a"

    # 4. Least Remaining Quota (Session) -> Account-A (5.0 vs 50.0)
    pool._strategy = "least_remaining_quota:session"
    chosen, _ = pool._select_unlocked(refresh=False, count=False)
    assert chosen.id == "id-a"

    # 5. Most Remaining Quota (Weekly) -> Account-B (90.0 vs 10.0)
    pool._strategy = "most_remaining_quota:weekly"
    chosen, _ = pool._select_unlocked(refresh=False, count=False)
    assert chosen.id == "id-b"

    # 6. Most Remaining Quota (Session) -> Account-B (50.0 vs 5.0)
    pool._strategy = "most_remaining_quota:session"
    chosen, _ = pool._select_unlocked(refresh=False, count=False)
    assert chosen.id == "id-b"


def test_cooldown_and_stale_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    now = datetime.now(timezone.utc)
    cache_file = tmp_path / "quota_cache.json"

    future_cooldown = (now + timedelta(hours=1)).isoformat()
    cache_data = {
        "fetched_at": now.isoformat(),
        "providers": {
            "test-provider": {
                "accounts": [
                    {
                        "label": "Account-A",
                        "cooldown_until": future_cooldown,
                        "windows": [{"label": "weekly", "remaining": 10.0}],
                    },
                    {
                        "label": "Account-B",
                        "windows": [{"label": "weekly", "remaining": 50.0}],
                    },
                ]
            }
        },
    }
    cache_file.write_text(json.dumps(cache_data))

    c1 = make_cred("id-a", label="Account-A", priority=0)
    c2 = make_cred("id-b", label="Account-B", priority=1)

    pool = CredentialPool(
        provider="test-provider",
        entries=[c1, c2],
    )

    pool._strategy = "least_remaining_quota:weekly"
    chosen, _ = pool._select_unlocked(refresh=False, count=False)
    # Account-A is in cooldown, so Account-B is chosen
    assert chosen.id == "id-b"


def test_delegation_provider_backend():
    backend = SubscriptionRouterBackend()
    # Strategy/window are plugin settings (delegation.backends.subscription-router),
    # not per-task values.
    config = {
        "provider": "test-provider",
        "backends": {
            "subscription-router": {"strategy": "Most Remaining Quota", "window": "session"}
        },
    }
    route = backend.route_task({}, config)
    assert "Most Remaining Quota (session)" in route["label"]
    assert route["provider"] == "test-provider"


def test_invalid_window_refused():
    backend = SubscriptionRouterBackend()
    config = {
        "provider": "test-provider",
        "backends": {"subscription-router": {"window": "fortnightly"}},
    }
    import pytest as _pytest
    with _pytest.raises(subscription_router.DelegationRouteError):
        backend.route_task({}, config)


def test_real_quota_cache_shape(quota_setup, monkeypatch):
    """Regression test against the production quota-cache shape: windows carry
    `used_percent` (not `remaining`) plus `reset_at`, with label variants
    'Weekly'/'Session'. Mirrors the live openai-codex two-account layout."""
    monkeypatch.setenv("HERMES_HOME", str(quota_setup))
    now = datetime.now(timezone.utc)
    cache_data = {
        "fetched_at": now.isoformat(),
        "providers": {
            "test-provider": {
                "accounts": [
                    {
                        "label": "account-a@example.com",
                        "windows": [
                            {"label": "Session", "reset_at": (now + timedelta(hours=3)).isoformat(), "used_percent": 0.0},
                            {"label": "Weekly", "reset_at": (now + timedelta(days=2)).isoformat(), "used_percent": 100.0},
                        ],
                    },
                    {
                        "label": "account-b",
                        "windows": [
                            {"label": "Session", "reset_at": (now + timedelta(hours=1)).isoformat(), "used_percent": 100.0},
                            {"label": "Weekly", "reset_at": (now + timedelta(days=5)).isoformat(), "used_percent": 44.0},
                        ],
                    },
                ]
            }
        },
    }
    (quota_setup / "quota_cache.json").write_text(json.dumps(cache_data))

    c1 = make_cred("id-a", label="account-a@example.com", priority=0)
    c2 = make_cred("id-b", label="account-b", priority=1)
    pool = CredentialPool(provider="test-provider", entries=[c1, c2])

    # Most remaining quota (weekly): account-a has 0% left, account-b 56% -> account-b.
    pool._strategy = "most_remaining_quota:weekly"
    chosen, _ = pool._select_unlocked(refresh=False, count=False)
    assert chosen.id == "id-b"

    # Least remaining quota (weekly): account-a has 0% left -> account-a.
    pool._strategy = "least_remaining_quota:weekly"
    chosen, _ = pool._select_unlocked(refresh=False, count=False)
    assert chosen.id == "id-a"

    # Ending soonest (weekly): account-a resets in 2 days vs account-b 5 -> account-a.
    pool._strategy = "ending_soonest:weekly"
    chosen, _ = pool._select_unlocked(refresh=False, count=False)
    assert chosen.id == "id-a"

    # Ending soonest (session): account-b resets in 1h vs account-a 3h -> account-b.
    pool._strategy = "ending_soonest:session"
    chosen, _ = pool._select_unlocked(refresh=False, count=False)
    assert chosen.id == "id-b"


def test_provider_without_window_data_abstains(tmp_path, monkeypatch):
    """A provider record with empty windows (e.g. openrouter) must abstain so
    the pool keeps its default order instead of erroring."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    now = datetime.now(timezone.utc)
    cache_data = {
        "fetched_at": now.isoformat(),
        "providers": {"test-provider": {"windows": [], "label": "x"}},
    }
    (tmp_path / "quota_cache.json").write_text(json.dumps(cache_data))
    c1 = make_cred("id-a", label="A", priority=0)
    c2 = make_cred("id-b", label="B", priority=1)
    pool = CredentialPool(provider="test-provider", entries=[c1, c2])
    pool._strategy = "most_remaining_quota:weekly"
    chosen, _ = pool._select_unlocked(refresh=False, count=False)
    assert chosen.id == "id-a"  # default order preserved
