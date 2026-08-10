"""Sign a built .mcpb with the pinned upstream mcpb CLI, then check the result is valid.

The published npm CLI (2.1.2) appends its signature block past the zip end-of-central-
directory record without updating that record's comment-length field, producing an archive
that strict readers — Claude Desktop among them — refuse with "Invalid comment length".
Upstream fixed that in PR #204, which has never been released.

`mcpb_cli.sh` builds the CLI from a pinned commit that carries the fix, so signing is done
by upstream's own corrected implementation rather than by patching its output here. The
pinned SHA is also the integrity control: it is a hash of the tree, which an npm version
range is not.

Note the built CLI still reports `--version` 2.1.2, because main was never version-bumped.
Never read the version string as evidence the fix is present — the structural check below,
and `verify.py`, are what actually establish it.

    https://github.com/modelcontextprotocol/mcpb/issues/278
"""

import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EOCD_MAGIC = b"PK\x05\x06"
EOCD_FIXED_SIZE = 22


def mcpb_cli() -> str:
    """Path to the CLI built from the pinned upstream commit."""
    built = subprocess.run(
        ["bash", str(HERE / "mcpb_cli.sh")], check=True, capture_output=True, text=True
    )
    return built.stdout.strip()


def signature_is_declared(mcpb: Path) -> bool:
    """A signed bundle must declare its trailing bytes or strict zip readers reject it."""
    raw = mcpb.read_bytes()
    eocd = raw.rfind(EOCD_MAGIC)
    declared = struct.unpack("<H", raw[eocd + 20 : eocd + 22])[0]
    trailing = len(raw) - (eocd + EOCD_FIXED_SIZE)
    return trailing > 0 and declared == trailing


def main() -> None:
    mcpb = Path(sys.argv[1]).resolve()
    cert, key = sys.argv[2], sys.argv[3]
    intermediate = sys.argv[4] if len(sys.argv) > 4 else None

    cmd = ["node", mcpb_cli(), "sign", str(mcpb), "--cert", cert, "--key", key]
    if intermediate:
        cmd += ["--intermediate", intermediate]
    subprocess.run(cmd, check=True)

    if not signature_is_declared(mcpb):
        raise SystemExit(
            "the signed bundle does not declare its signature in the archive comment "
            "length, so Claude Desktop will refuse it. The CLI that ran does not carry "
            "the PR #204 fix — check mcpb_cli.sh."
        )
    print("  signed; the archive declares the signature block")
    print("  note: `mcpb verify` cannot confirm this — node-forge never implemented")
    print("        PKCS#7 verification. verify.py is the structural check.")


if __name__ == "__main__":
    main()
