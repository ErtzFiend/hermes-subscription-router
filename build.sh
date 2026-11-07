#!/usr/bin/env bash
# Builds the ready-to-install release archives (tar.gz + zip) and SHA256SUMS.
#
#   ./build.sh              # packages only
#   RUN_TESTS=1 ./build.sh  # also run the integration tests (needs HERMES_SRC)
set -euo pipefail
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PLUGIN_DIR"
VERSION="$(grep '^version:' plugin.yaml | sed 's/version:[[:space:]]*//')"
NAME="subscription-router"
ARCHIVE_BASE="dist/${NAME}-${VERSION}"

if [[ "${RUN_TESTS:-0}" == "1" ]]; then
  ./run_tests.sh
fi

echo "==> Building distribution archives..."
rm -rf dist
mkdir -p dist

# Stage the runtime tree under a top-level $NAME/ folder so the archive
# extracts to $HERMES_HOME/plugins/$NAME regardless of the checkout's own
# directory name (GitHub checks repos out as <repo>/, not <plugin>/).
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$NAME"
(cd "$PLUGIN_DIR" && tar --exclude="./dist" --exclude="./tests" --exclude="./patches" \
    --exclude="./.git" --exclude="./.github" --exclude="__pycache__" --exclude="*.pyc" \
    --exclude="./.venv" --exclude="./.pytest_cache" -cf - .) | tar -xf - -C "$STAGE/$NAME"

tar -czf "${ARCHIVE_BASE}.tar.gz" -C "$STAGE" "$NAME"

NAME="$NAME" STAGE="$STAGE" PLUGIN_DIR="$PLUGIN_DIR" VERSION="$VERSION" python3 - <<'PYEOF'
import os, zipfile
name = os.environ["NAME"]
version = os.environ["VERSION"]
stage = os.environ["STAGE"]
out = os.path.join(os.environ["PLUGIN_DIR"], "dist", f"{name}-{version}.zip")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for dirpath, dirnames, filenames in os.walk(os.path.join(stage, name)):
        for f in filenames:
            p = os.path.join(dirpath, f)
            z.write(p, os.path.relpath(p, stage))
print("zip written:", out)
PYEOF

( cd dist && sha256sum "${NAME}-${VERSION}.tar.gz" "${NAME}-${VERSION}.zip" > SHA256SUMS )
echo "==> Archive structure:"
tar -tzf "${ARCHIVE_BASE}.tar.gz"
echo "==> Build complete:"
ls -lh dist/
