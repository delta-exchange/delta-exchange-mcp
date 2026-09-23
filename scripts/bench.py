"""Startup/memory/context cost bench for the MCP server. stdlib only.

Usage:
  uv run python scripts/bench.py                 # spawn + measure, no-key and fake-key, N=5
  uv run python scripts/bench.py --runs 3
  uv run python scripts/bench.py --importtime     # python -X importtime breakdown
  uv run python scripts/bench.py --api            # live list_products / get_orderbook byte sizes

Talks raw JSON-RPC over stdio to `python -m delta_exchange_mcp` (the entrypoint declared in
pyproject.toml's [project.scripts], run the same way via `-m` since that always resolves
against this checkout rather than whatever `delta-exchange-mcp` is on PATH). No test
credentials are ever real: DELTA_API_KEY=x / DELTA_API_SECRET=y below just arm the
authenticated tool surface so its extra tools/list weight can be measured — they are never
sent over the network in this script.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _rss_bytes(ru_maxrss: int) -> int:
    """ru_maxrss is bytes on macOS/BSD, KiB on Linux."""
    return ru_maxrss if sys.platform == "darwin" else ru_maxrss * 1024


def _rpc(msg_id: int | None, method: str, params: dict | None = None) -> dict:
    payload: dict = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        payload["id"] = msg_id
    if params is not None:
        payload["params"] = params
    return payload


def _write(proc: subprocess.Popen, obj: dict) -> None:
    line = json.dumps(obj) + "\n"
    assert proc.stdin is not None
    proc.stdin.write(line.encode())
    proc.stdin.flush()


def _read_reply(
    proc: subprocess.Popen, want_id: int, timeout: float = 20.0
) -> tuple[dict, int]:
    """Read lines until one with id == want_id (skipping notifications). Returns (obj, bytes)."""
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout before replying")
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("id") == want_id:
            return obj, len(line)
    raise TimeoutError(f"no reply with id={want_id} within {timeout}s")


def _env_for(no_creds: bool, fake_creds: bool, home_dir: str) -> dict:
    env = dict(os.environ)
    env["HOME"] = home_dir
    # Belt-and-suspenders: keep the bench isolated from any real ~/.delta-exchange-mcp
    # even if HOME's override is bypassed by something upstream.
    env["DELTA_MCP_CONFIG_FILE"] = str(Path(home_dir) / "config.env")
    for k in ("DELTA_API_KEY", "DELTA_API_SECRET"):
        env.pop(k, None)
    if fake_creds:
        env["DELTA_API_KEY"] = "x"
        env["DELTA_API_SECRET"] = "y"
    return env


def run_once(fake_creds: bool) -> dict:
    with tempfile.TemporaryDirectory() as home_dir:
        env = _env_for(
            no_creds=not fake_creds, fake_creds=fake_creds, home_dir=home_dir
        )
        t0 = time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, "-m", "delta_exchange_mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO_ROOT),
            env=env,
        )
        try:
            _write(
                proc,
                _rpc(
                    1,
                    "initialize",
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "bench", "version": "0"},
                    },
                ),
            )
            _read_reply(proc, 1)
            _write(proc, _rpc(None, "notifications/initialized"))

            _write(proc, _rpc(2, "tools/list"))
            tools_reply, tools_bytes = _read_reply(proc, 2)
            t_tools = time.monotonic()

            extra_bytes = {"prompts/list": None, "resources/list": None}
            next_id = 3
            for method in list(extra_bytes):
                _write(proc, _rpc(next_id, method))
                try:
                    _reply, n = _read_reply(proc, next_id, timeout=3.0)
                    extra_bytes[method] = n
                except (TimeoutError, RuntimeError):
                    extra_bytes[method] = None
                next_id += 1

            wall_ms = (t_tools - t0) * 1000
            n_tools = len(tools_reply.get("result", {}).get("tools", []))
        finally:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        return {
            "wall_ms": wall_ms,
            "tools_list_bytes": tools_bytes,
            "tools_list_tokens_approx": tools_bytes / 4,
            "n_tools": n_tools,
            "prompts_list_bytes": extra_bytes["prompts/list"],
            "resources_list_bytes": extra_bytes["resources/list"],
            "peak_rss_bytes": _rss_bytes(usage.ru_maxrss),
        }


def per_tool_schema_bytes(fake_creds: bool) -> list[tuple[str, int]]:
    """Bytes of each tool's own JSON schema entry in tools/list, largest first."""
    with tempfile.TemporaryDirectory() as home_dir:
        env = _env_for(
            no_creds=not fake_creds, fake_creds=fake_creds, home_dir=home_dir
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "delta_exchange_mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO_ROOT),
            env=env,
        )
        try:
            _write(
                proc,
                _rpc(
                    1,
                    "initialize",
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "bench", "version": "0"},
                    },
                ),
            )
            _read_reply(proc, 1)
            _write(proc, _rpc(None, "notifications/initialized"))
            _write(proc, _rpc(2, "tools/list"))
            reply, _ = _read_reply(proc, 2)
        finally:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    tools = reply.get("result", {}).get("tools", [])
    sizes = [(t["name"], len(json.dumps(t))) for t in tools]
    sizes.sort(key=lambda x: -x[1])
    return sizes


def bench(runs: int) -> None:
    print(f"platform={sys.platform} python={sys.version.split()[0]} runs={runs}\n")
    for label, fake in (("no-key", False), ("fake-key", True)):
        rows = [run_once(fake) for _ in range(runs)]
        wall = statistics.median(r["wall_ms"] for r in rows)
        rss = statistics.median(r["peak_rss_bytes"] for r in rows)
        tb = statistics.median(r["tools_list_bytes"] for r in rows)
        tt = statistics.median(r["tools_list_tokens_approx"] for r in rows)
        n = rows[0]["n_tools"]
        print(f"[{label}] median over {runs} runs")
        print(f"  spawn->tools/list: {wall:.1f} ms")
        print(f"  peak RSS (children): {rss / 1e6:.1f} MB")
        print(f"  tools/list: {int(tb)} bytes, ~{tt:.0f} tokens, {n} tools")
        print(f"  prompts/list bytes: {rows[0]['prompts_list_bytes']}")
        print(f"  resources/list bytes: {rows[0]['resources_list_bytes']}")
        print()

    print("Top 10 heaviest tool schemas (fake-key surface, JSON bytes):")
    for name, size in per_tool_schema_bytes(fake_creds=True)[:10]:
        print(f"  {size:6d}  {name}")


def importtime() -> None:
    proc = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", "import delta_exchange_mcp.server"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    lines = [
        line for line in proc.stderr.splitlines() if line.startswith("import time:")
    ]
    rows = []
    for line in lines[1:]:  # skip header
        try:
            _, rest = line.split(":", 1)
            self_us, cum_us, name = rest.split("|")
            rows.append((int(self_us.strip()), int(cum_us.strip()), name.strip()))
        except ValueError:
            continue
    rows.sort(key=lambda r: -r[0])
    print("Top 15 modules by self import time (us):")
    for self_us, cum_us, name in rows[:15]:
        print(f"  self={self_us:6d}us cum={cum_us:7d}us  {name}")


def api_bytes() -> None:
    """Live response size of list_products (default args) and get_orderbook (default)."""
    base = "https://api.india.delta.exchange/v2"
    for label, path, params in (
        ("list_products(default)", "/products", "page_size=100"),
        ("get_orderbook(BTCUSD,default)", "/l2orderbook/BTCUSD", ""),
    ):
        url = f"{base}{path}" + (f"?{params}" if params else "")
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                body = resp.read()
            print(f"  {label}: {len(body)} bytes, ~{len(body) / 4:.0f} tokens  ({url})")
        except Exception as exc:  # noqa: BLE001 - best-effort network probe, not test code
            print(f"  {label}: FAILED ({exc})  ({url})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--importtime", action="store_true")
    parser.add_argument("--api", action="store_true")
    args = parser.parse_args()

    if args.importtime:
        importtime()
        return
    if args.api:
        api_bytes()
        return
    bench(args.runs)


if __name__ == "__main__":
    main()
