"""Check a built .mcpb: archive structure, then a real MCP handshake from a fresh unpack.

Packing successfully is not evidence the bundle works. This unpacks the artifact the way a
client would and speaks the protocol to it, so a bundle that installs but cannot start
fails here rather than on someone's machine.
"""

import json
import os
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

MUTATING_TOOL_META_KEY = "delta.exchange/mutating"
MODERN_PROTOCOL = "2026-07-28"
LEGACY_PROTOCOL = "2025-06-18"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
EXPECTED_TOOL_NAMES = frozenset(
    {
        "adjust_position_margin",
        "bulk_fills_export",
        "cancel_all_orders",
        "cancel_batch_orders",
        "cancel_order",
        "close_all_positions",
        "configure_auto_topup",
        "edit_batch_orders",
        "edit_bracket_order",
        "edit_order",
        "get_candles",
        "get_connection_status",
        "get_debug_status",
        "get_fills",
        "get_funding_history",
        "get_indices",
        "get_margined_positions",
        "get_mark_price_history",
        "get_oi_history",
        "get_open_orders",
        "get_options_chain",
        "get_order_by_id",
        "get_order_history",
        "get_orderbook",
        "get_positions",
        "get_product",
        "get_product_leverage",
        "get_recent_trades",
        "get_reference_data",
        "get_settlement_prices",
        "get_ticker",
        "get_trading_preferences",
        "get_trading_stats",
        "get_trading_status",
        "get_wallet_balances",
        "get_wallet_transactions",
        "list_products",
        "list_tickers",
        "place_batch_orders",
        "place_bracket_order",
        "place_order",
        "set_product_leverage",
        "setup_credentials",
    }
)
RETIRED_TOOL_NAMES = frozenset({"get_profile", "save_credentials", "save_mode"})


def check_archive(mcpb: Path) -> None:
    """Trailing bytes must be declared in the EOCD comment length or strict readers refuse.

    Claude Desktop uses a strict zip parser. A signature appended past the end-of-central-
    directory record without updating that field yields "Invalid comment length" at install,
    while lenient readers (Python, Info-ZIP) open the same file happily.
    """
    raw = mcpb.read_bytes()
    eocd = raw.rfind(b"PK\x05\x06")
    if eocd == -1:
        raise SystemExit("not a zip: no end-of-central-directory record")
    declared = struct.unpack("<H", raw[eocd + 20 : eocd + 22])[0]
    trailing = len(raw) - (eocd + 22)
    if declared != trailing:
        raise SystemExit(
            f"archive is not strict-parser valid: EOCD declares a {declared}-byte comment "
            f"but {trailing} bytes follow it"
        )

    with zipfile.ZipFile(mcpb) as z:
        bad = z.testzip()
        if bad is not None:
            raise SystemExit(f"corrupt entry: {bad}")
        names = set(z.namelist())

    # Assert the payload rather than trusting .mcpbignore. Build tooling sits beside the
    # payload in this directory, so one missed ignore rule would otherwise ship it silently.
    required = {"manifest.json", "pyproject.toml", "uv.lock", "icon.png", "server/main.py"}
    missing = required - names
    if missing:
        raise SystemExit(f"missing from the bundle: {', '.join(sorted(missing))}")

    wheels = {n for n in names if n.startswith("wheels/") and n.endswith(".whl")}
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one vendored wheel, found {len(wheels)}")

    unexpected = names - required - wheels
    if unexpected:
        raise SystemExit(
            "unexpected files in the bundle (build tooling leaking through "
            f".mcpbignore?): {', '.join(sorted(unexpected))}"
        )

    print(f"  archive: {len(raw)} bytes, {len(names)} entries, CRCs OK, strict-parser valid")
    print(f"  payload: {', '.join(sorted(required))}, {wheels.pop()}")


def launch_env(workdir: Path) -> dict[str, str]:
    """Build a disposable process environment with hostile legacy authorization values."""
    env = dict(os.environ)
    env.update({
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        "DELTA_MCP_MODE": "trade",
        "DELTA_MCP_ENV": "india_prod",
        "DELTA_API_KEY": "ambient",
        "DELTA_API_SECRET": "ambient",
        "DELTA_MCP_DEBUG": "1",
        "DELTA_MCP_DEBUG_FILE": str(workdir / "debug.log"),
        "DELTA_MCP_AUDIT_FILE": str(workdir / "audit.log"),
        "DELTA_MCP_CONFIG_FILE": str(workdir / "shared-config.env"),
    })
    return env


def _pump(stream, put) -> None:
    """Move one of the child's output pipes somewhere the main thread can reach it.

    Reading a pipe directly blocks until a newline arrives or the writer closes it, with no
    way to give up. A bundle that starts but never answers is precisely what this verifier
    exists to catch, and read inline it would hold the build until the runner's own job
    timeout hours later instead of failing on the deadline below. Draining stderr matters
    for the same reason from the other direction: a child that fills the stderr pipe buffer
    while nobody reads it blocks before it ever replies on stdout.
    """
    try:
        for line in iter(stream.readline, ""):
            put(line)
    finally:
        put(None)


def handshake(
    extracted: Path,
    *,
    modern: bool,
    env: dict[str, str] | None = None,
    timeout: float = 240.0,
) -> dict[str, dict]:
    """Discover one fresh unpack and return its tools by name."""
    proc = subprocess.Popen(
        ["uv", "run", "--directory", str(extracted), "--frozen", "python", "server/main.py"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )

    replies: queue.Queue = queue.Queue()
    errors: list[str] = []
    readers = [
        threading.Thread(target=_pump, args=(proc.stdout, replies.put), daemon=True),
        threading.Thread(
            target=_pump, args=(proc.stderr, lambda line: line and errors.append(line)), daemon=True
        ),
    ]
    for reader in readers:
        reader.start()

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    client_info = {"name": "bundle-verify", "version": "1"}
    request_meta = {
        PROTOCOL_VERSION_META_KEY: MODERN_PROTOCOL,
        CLIENT_INFO_META_KEY: client_info,
        CLIENT_CAPABILITIES_META_KEY: {},
    }
    if modern:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"_meta": request_meta},
            }
        )
    else:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LEGACY_PROTOCOL,
                    "capabilities": {},
                    "clientInfo": client_info,
                },
            }
        )

    deadline = time.time() + timeout
    seen: dict[int, dict] = {}
    asked = False
    while 2 not in seen:
        try:
            line = replies.get(timeout=max(0.0, deadline - time.time()))
        except queue.Empty:  # the deadline passed with the child still alive and silent
            break
        if line is None:  # the child closed stdout
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(msg.get("id"), int):
            seen[msg["id"]] = msg
        if msg.get("id") == 1 and not asked:
            asked = True
            if not modern:
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    **({"params": {"_meta": request_meta}} if modern else {}),
                }
            )

    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    # Give the readers a moment to finish now the writer is gone, so the diagnostics below
    # carry everything the child managed to say rather than whatever had arrived by then.
    for reader in readers:
        reader.join(timeout=5)

    tail = "".join(errors)[-2000:]
    if 1 not in seen:
        operation = "server/discover" if modern else "initialize"
        raise SystemExit(f"no {operation} response\nstderr:\n{tail}")
    if 2 not in seen:
        raise SystemExit(f"no tools/list response\nstderr:\n{tail}")

    if modern:
        supported = seen[1].get("result", {}).get("supportedVersions", [])
        if MODERN_PROTOCOL not in supported:
            raise SystemExit(
                f"server/discover did not advertise {MODERN_PROTOCOL}: {supported!r}"
            )
        print(f"  discovery: server/discover OK, supported={supported}")
    else:
        info = seen[1]["result"].get("serverInfo", {})
        print(f"  discovery: initialize OK, serverInfo={info}")
    return {tool["name"]: tool for tool in seen[2]["result"]["tools"]}


def mutation_names(tools: dict[str, dict]) -> list[str]:
    """Return tools whose registration explicitly identifies them as mutating."""
    return sorted(
        name
        for name, tool in tools.items()
        if tool.get("_meta", {}).get(MUTATING_TOOL_META_KEY) is True
    )


def main() -> None:
    mcpb = Path(sys.argv[1]).resolve()
    print(f"verifying {mcpb.name}")
    check_archive(mcpb)

    tmp = Path(tempfile.mkdtemp(prefix="mcpb-verify-"))
    try:
        with zipfile.ZipFile(mcpb) as z:
            z.extractall(tmp)
        manifest = json.loads((tmp / "manifest.json").read_text())

        if "user_config" in manifest:
            raise SystemExit("the browser-configured bundle must not declare user_config prompts")
        if "env" in manifest["server"]["mcp_config"]:
            raise SystemExit("the browser-configured bundle must not inject launch credentials")

        env = launch_env(tmp)
        modern = handshake(tmp, modern=True, env=env)
        legacy = handshake(tmp, modern=False, env=env)
        if not modern:
            raise SystemExit("no tools registered")
        if set(modern) != set(legacy):
            raise SystemExit(
                "modern and legacy discovery returned different tool lists: "
                f"modern-only={sorted(set(modern) - set(legacy))}, "
                f"legacy-only={sorted(set(legacy) - set(modern))}"
            )
        declared = {t["name"] for t in manifest["tools"]}
        runtime = set(modern)
        retired = (declared | runtime) & RETIRED_TOOL_NAMES
        if retired:
            raise SystemExit(
                f"retired setup tools must stay absent: {sorted(retired)}"
            )
        if declared != runtime:
            raise SystemExit(
                "manifest and runtime tool lists differ: "
                f"runtime-only={sorted(runtime - declared)}, "
                f"manifest-only={sorted(declared - runtime)}"
            )
        if runtime != EXPECTED_TOOL_NAMES:
            raise SystemExit(
                "runtime tool list differs from the approved stable registry: "
                f"new={sorted(runtime - EXPECTED_TOOL_NAMES)}, "
                f"missing={sorted(EXPECTED_TOOL_NAMES - runtime)}"
            )
        mutations = mutation_names(modern)
        if len(mutations) != 13:
            raise SystemExit(
                f"expected 13 annotated trading tools, found {len(mutations)}: {mutations}"
            )
        print(f"  stable tools: {len(runtime)} total, {len(mutations)} trading")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("  OK")


if __name__ == "__main__":
    main()
