#!/usr/bin/env bash
# Launch MCP Inspector against the delta-exchange-mcp stdio server.
#
# Two modes:
#
#   Web UI mode (default) - opens UI on :6274 and proxy on :6277.
#     bash scripts/inspect.sh
#   With creds:
#     DELTA_API_KEY=... DELTA_API_SECRET=... bash scripts/inspect.sh
#
#   CLI mode - headless, works over SSH (no browser needed).
#     bash scripts/inspect.sh --cli --method tools/list
#     bash scripts/inspect.sh --cli --method tools/call \
#       --tool-name get_ticker --tool-arg symbol=BTCUSD
#
# Inspector v2 wants the mode flag first and the server command before every option,
# so the layout is:
#   inspector <mode> <server-cmd> [<server-args>...] [-e KEY=VAL ...] [--method ...]

set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"

# v1 took the mode flag last. v2 forwards a trailing --cli to the server, so the web UI
# opened instead of the CLI and the server exited 2 on the unknown flag.
MODE="--web"
case "${1:-}" in
  --cli|--tui|--web)
    MODE="$1"
    shift
    ;;
esac

# The Inspector refuses to bind 0.0.0.0: its backend spawns local processes, which is what
# DNS-rebinding attacks target. For remote access: ssh -L 6274:localhost:6274 <host>
export HOST="${HOST:-127.0.0.1}"
export CLIENT_PORT="${CLIENT_PORT:-6274}"
export SERVER_PORT="${SERVER_PORT:-6277}"

ENV_ARGS=(
  -e "DELTA_MCP_ENV=${DELTA_MCP_ENV:-india_testnet}"
)
if [[ -n "${DELTA_API_KEY:-}" ]]; then
  ENV_ARGS+=(-e "DELTA_API_KEY=${DELTA_API_KEY}")
fi
if [[ -n "${DELTA_API_SECRET:-}" ]]; then
  ENV_ARGS+=(-e "DELTA_API_SECRET=${DELTA_API_SECRET}")
fi

exec npx --yes @modelcontextprotocol/inspector \
  "$MODE" \
  uv run delta-exchange-mcp \
  "${ENV_ARGS[@]}" \
  "$@"
