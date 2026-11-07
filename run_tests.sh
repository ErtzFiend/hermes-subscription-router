#!/usr/bin/env bash
# Runs the subscription-router integration tests against a Hermes checkout.
#
#   HERMES_SRC=/path/to/hermes-agent ./run_tests.sh
#
# HERMES_SRC must contain agent/credential_pool.py WITH the credential-pool
# strategy seam applied (see patches/router-core-seam.patch in this repo).
# A disposable HERMES_HOME is used; nothing in ~/.hermes is touched.
set -euo pipefail
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_SRC="${HERMES_SRC:-/usr/local/lib/hermes-agent}"

if [[ ! -f "$HERMES_SRC/agent/credential_pool.py" ]]; then
  echo "ERROR: Hermes source not found at $HERMES_SRC (set HERMES_SRC)" >&2
  exit 2
fi
if ! grep -q "register_credential_pool_strategy" "$HERMES_SRC/agent/credential_pool.py"; then
  echo "ERROR: $HERMES_SRC/agent/credential_pool.py lacks the credential-pool strategy seam." >&2
  echo "       Apply patches/router-core-seam.patch first (see README.md)." >&2
  exit 2
fi

command -v python3 >/dev/null || { echo "ERROR: python3 not found" >&2; exit 2; }
python3 -c "import pytest" 2>/dev/null || {
  echo "ERROR: pytest not importable by python3 (pip install pytest ruamel.yaml pyyaml)" >&2
  exit 2
}

echo "Running subscription-router integration tests (HERMES_SRC=$HERMES_SRC)..."
HERMES_HOME="$(mktemp -d)" \
PYTHONPATH="$HERMES_SRC" \
PYTHONDONTWRITEBYTECODE=1 \
  python3 -m pytest "$PLUGIN_DIR/tests/test_subscription_router.py" "$@"
echo "All tests passed."
