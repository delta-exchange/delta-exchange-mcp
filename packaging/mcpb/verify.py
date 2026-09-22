"""Check a built .mcpb: archive structure, then a real MCP handshake from a fresh unpack.

Packing successfully is not evidence the bundle works. This unpacks the artifact the way a
client would and speaks the protocol to it, so a bundle that installs but cannot start
fails here rather than on someone's machine.
"""

import json
import os
import queue
import re
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


def launch_env(manifest: dict, mode: str, workdir: Path) -> dict[str, str]:
    """The environment a host would build, over a deliberately hostile one.

    The ambient half sets DELTA_MCP_MODE=trade and supplies credentials, which is what a
    machine with those exported looks like. The manifest half is then applied on top with
    ${user_config.x} resolved the way the host resolves it. Checking the result is what
    makes "the form decides the mode, not the environment" an actual test rather than an
    assertion that passes because no credentials were present.

    DELTA_MCP_DEBUG is in the ambient half and *not* declared by the manifest, which is the
    point: the manifest env is applied over the user's environment, so an undeclared variable
    reaches the server untouched and registers `get_debug_status`. Left out of here, the
    undeclared-tool check in `main` could only ever pass, because CI's own shell has no such
    variable. With it, that check is what proves the declared list is a real ceiling.

    Everything the server writes is pointed at `workdir`, the throwaway unpack: the debug log
    that turning debug on creates, the audit log that trade mode with credentials opens, and
    the shared settings file. That last one is not tidiness — the server reads
    ~/.delta-exchange-mcp/config.env for anything the manifest does not declare, so a
    developer with DELTA_MCP_DEBUG=1 in their own file would fail the undeclared-tool check
    here for a reason CI could never reproduce. Left at their defaults, every build also
    wrote three files into a home directory a build has no business touching.
    """
    config = {k: v.get("default", "") for k, v in manifest["user_config"].items()}
    config.update({"mode": mode, "api_key": "placeholder", "api_secret": "placeholder"})

    env = dict(os.environ)
    env.update({
        "DELTA_MCP_MODE": "trade",
        "DELTA_API_KEY": "ambient",
        "DELTA_API_SECRET": "ambient",
        "DELTA_MCP_DEBUG": "1",
        "DELTA_MCP_DEBUG_FILE": str(workdir / "debug.log"),
        "DELTA_MCP_AUDIT_FILE": str(workdir / "audit.log"),
        "DELTA_MCP_CONFIG_FILE": str(workdir / "shared-config.env"),
    })
    for key, raw in manifest["server"]["mcp_config"]["env"].items():
        env[key] = re.sub(
            r"\$\{user_config\.(\w+)\}", lambda m: str(config.get(m.group(1), "")), raw
        )
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

        modern = handshake(
            tmp,
            modern=True,
            env=launch_env(manifest, manifest["user_config"]["mode"]["default"], tmp),
        )
        legacy = handshake(
            tmp,
            modern=False,
            env=launch_env(manifest, "trade", tmp),
        )
        if set(modern) != set(legacy):
            raise SystemExit(
                "modern and legacy discovery returned different tool lists: "
                f"modern-only={sorted(set(modern) - set(legacy))}, "
                f"legacy-only={sorted(set(legacy) - set(modern))}"
            )
        if not modern:
            raise SystemExit("no tools registered")

        declared = {t["name"] for t in manifest["tools"]}
        runtime = set(modern)
        if declared != runtime:
            raise SystemExit(
                "manifest and runtime tool lists differ: "
                f"runtime-only={sorted(runtime - declared)}, "
                f"manifest-only={sorted(declared - runtime)}"
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
