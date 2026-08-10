#!/usr/bin/env bash
# Build and verify the one-click .mcpb bundle from the repo source.
#
#   bash packaging/mcpb/build.sh
#
# Signing is separate and needs a certificate — see sign.py and the README.
# Everything shared with the published package (version, dependency ceilings, licence,
# URLs, the Python floor) is derived from the repo's pyproject.toml by make_bundle.py.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PY=(uv run --no-project python)

VERSION="$("${PY[@]}" "$HERE/make_bundle.py" version)"
# Built from a pinned upstream commit, not the npm release — the published CLI signs
# bundles Claude Desktop refuses. See mcpb_cli.sh.
MCPB=(node "$(bash "$HERE/mcpb_cli.sh")")
OUT="$HERE/delta-exchange-mcp-${VERSION}.mcpb"

echo "==> building wheel ${VERSION}"
rm -rf "$HERE/wheels"
mkdir -p "$HERE/wheels"
uv build --wheel --out-dir "$HERE/wheels" --project "$REPO" >/dev/null

echo "==> generating the bundle project from the repo's metadata"
"${PY[@]}" "$HERE/make_bundle.py" pyproject

echo "==> locking"
rm -f "$HERE/uv.lock"
uv lock --directory "$HERE" >/dev/null

echo "==> generating the manifest from the live tool list"
uv run --directory "$HERE" --frozen python make_bundle.py manifest

echo "==> packing"
rm -rf "$HERE/.venv" "$HERE"/*.mcpb
cd "$HERE"
# Check validate's own exit status, not the pipeline's. Piping through grep and adding
# `|| true` — to stop grep's "no lines matched" exit killing the build — also swallowed a
# real rejection, so an invalid manifest would go straight on to be packed.
if ! validation="$("${MCPB[@]}" validate manifest.json 2>&1)"; then
  echo "$validation" | grep -v '^npm notice' >&2 || true
  echo "!!  manifest validation failed — refusing to pack." >&2
  exit 1
fi
echo "$validation" | grep -v '^npm notice' || true

"${MCPB[@]}" pack . "$OUT" 2>&1 | grep -v '^npm notice'

echo "==> verifying"
"${PY[@]}" "$HERE/verify.py" "$OUT"

echo
echo "built: $OUT"
