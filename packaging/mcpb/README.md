# One-click bundle (`.mcpb`)

Packages the server so a non-technical user installs it by double-clicking a file and
typing their API key into a form, instead of hand-editing `claude_desktop_config.json`.

Bundles are supported by **Claude Desktop, Claude Code, and MCP for Windows**. Cursor,
VS Code, Codex and Windsurf do not read `.mcpb` — they keep the existing `uvx` install.

## Build

```bash
bash packaging/mcpb/build.sh
```

Produces and verifies `packaging/mcpb/delta-exchange-mcp-<version>.mcpb`. The same script
runs in CI on every PR that touches `packaging/mcpb/`, `src/` or `pyproject.toml`, so a
local build and a release build are the same build.

## Files

| File | |
|---|---|
| `build.sh` | Orchestration: wheel, project, lock, manifest, pack, verify. |
| `make_bundle.py` | Generates `pyproject.toml` and `manifest.json`. **Edit copy here.** |
| `verify.py` | Checks a built bundle. Run by `build.sh` and by CI. |
| `mcpb_cli.sh` | Builds the mcpb CLI from a pinned upstream commit (see Caveats). |
| `sign.py` | Signs with that CLI, then checks the archive declares the signature. |
| `manifest.json` | Generated, **committed** — the user-facing contract. CI fails if stale. |
| `pyproject.toml`, `uv.lock`, `wheels/`, `*.mcpb` | Generated, not committed. |

## Nothing shared is written twice

`make_bundle.py` reads the repo's `pyproject.toml` for everything the bundle must agree
with: name, version, licence, URLs, the Python floor, and the dependency ceilings. Restating
any of those invites the two to drift, and it has already bitten once — the bundle used to
carry its own copy of `mcp<2`, which would have silently pinned below the SDK the moment the
project raised that ceiling.

What stays literal is the copy shown to someone installing the bundle: `display_name`, the
descriptions, and the three `user_config` field labels. That is deliberately different text
from the PyPI summary, which is written for developers. The rule: if two values must move
together, derive one from the other; if they would sensibly diverge, write both.

## What `verify.py` checks

Packing successfully is not evidence the bundle works, so `build.sh` will not report success
until all of this passes:

- the archive is valid to a **strict** zip parser, which is what Claude Desktop uses
- the packed payload is **exactly** the expected file set, so build tooling sitting beside
  it in this directory cannot leak in through a missed `.mcpbignore` rule
- two real MCP **handshakes** against a fresh unpack — `initialize`, then `tools/list`
- **the form decides the mode, not the environment**: accepting the declared default
  registers no mutation tool even when the ambient environment says `DELTA_MCP_MODE=trade`
- **mutations identify themselves**: every trading tool carries the namespaced
  `_meta["delta.exchange/mutating"]` marker, so the verifier does not infer safety from names
- **the opt-in works**: `trade` reaches all 13 mutation tools, so the field is not decorative
- **nothing undeclared**: every tool the server registers appears in the manifest, which is
  what `tools_generated: false` promises

Both handshakes start from a deliberately hostile environment — `DELTA_MCP_MODE=trade`,
credentials and `DELTA_MCP_DEBUG=1` exported — and then apply the manifest's `env` over it with
`${user_config.x}` resolved the way a host resolves it. Debug and audit output are redirected
into the throwaway unpack so a build never writes to `~/.delta-exchange-mcp`. That is what makes
the mode and undeclared-tool checks meaningful. An earlier version simply launched the server
with no credentials and asserted zero mutations, which passed because mutation tools are gated
on credentials *and* trade mode: it could not have failed. Confirm any change here still fails
when `DELTA_MCP_MODE` is removed from the manifest's `env`, which is the bug the check exists to
catch.

## The icon

`icon.png` is the Delta Exchange mark at 512x512, rendered from the vector source used by
`delta-exchange/api-console` (`app/favicon.svg`) with the viewBox widened to `-3 -3 36 36`
so it is not edge-to-edge at icon sizes:

```bash
rsvg-convert -w 512 -h 512 favicon.svg -o icon.png
```

## Install to test

Double-click the `.mcpb`, or drag it onto Claude Desktop. Leave the API fields empty for
market data only.

The first install of any `uv` bundle needs network and can be slow: the host resolves `uv`,
then runs `uv sync`, which fetches a Python interpreter and the dependencies. It surfaces
that as download progress.

The extension lands in `~/Library/Application Support/Claude/Claude Extensions/`, one
directory per extension, with the `.venv` that `uv sync` built alongside the shipped payload.
That directory and the app's own log are where to look when a bundle installs but will not
start.

## Decisions

**An empty form field falls through to the shared settings file.** The server reads
`~/.delta-exchange-mcp/config.env` for anything its environment does not answer, and a
bundle substitutes every variable it declares whether or not the user filled that field —
so a blank API-key box arrives as `""`. Empty therefore has to mean "unanswered" rather
than "answered with nothing", or the shared file could never reach a bundle user at all.
Someone who has already run `delta-exchange-mcp login` can accept the whole form and still
get their account.

`verify.py` redirects that file into its throwaway unpack directory. Without it the
verifier would read the developer's own copy, and a `DELTA_MCP_DEBUG=1` there would
register a debug tool the manifest does not declare — failing the undeclared-tool check
locally while passing in CI. It also keeps a build from writing into `$HOME`.

**Trading is present but opt-in.** `DELTA_MCP_MODE` is a `user_config` field defaulting to
`read`, substituted into the launch environment as `${user_config.mode}`. With credentials and
debug off, `read` gets 27 tools and no mutations; `trade` gets 41 and can place real orders.
The verifier intentionally enables debug and therefore sees 28 and 42 respectively, with
`get_debug_status` as the extra declared tool. `user_config` has no enum type — only string,
number, boolean, directory and file — so this is a free-text string, the same shape as the
`environment` field beside it.

Declaring the variable matters as much as its default. Leaving it out of the manifest entirely
would let an ambient `DELTA_MCP_MODE=trade` in the environment the app was launched with reach
the server and arm trading without the user choosing it. `verify.py` reproduces exactly that
and fails on it.

The default is `read` rather than `trade` because a bundle is a double-click and a form, so
whatever the default is becomes what most people run — and these tools have no notional cap,
size orders in contracts rather than coins, and act on the live exchange.

**`server.type: "uv"`.** No prerequisite on the user's machine — not `uv`, and not Python.
Claude Desktop resolves `uv` in three steps: it looks for a system installation, else reuses
a copy it downloaded earlier under `uv-runtime/`, else downloads one from the `astral-sh/uv`
releases and clears the macOS quarantine attribute. It then runs `uv sync` against the
`pyproject.toml` and `uv.lock` shipped inside the bundle, letting `uv` fetch an interpreter
meeting the `>=3.12` floor rather than requiring a system Python. Where the manifest says
`"command": "uv"` the app discards that string and launches whichever binary it resolved, by
absolute path — so nothing resolves against the user's `PATH` at launch, which matters
because an app started from Finder inherits the launchd environment rather than a shell one.

That removes the reason to consider `type: "binary"` with a PyInstaller executable. That
option existed only to drop a `uv` prerequisite which turns out not to exist, and it would
have cost four platform builds plus Apple notarization.

**Dependencies pinned and locked.** `uv.lock` ships inside the bundle and the launch line
passes `--frozen`, so nothing re-resolves at start-up. The `uv sync` the host runs at install
time is a separate step and is not `--frozen`, but it consumes that same shipped lock, which
is generated alongside the shipped `pyproject.toml` and so agrees with it.

## Caveats

- **Signing uses a CLI built from upstream, not the npm release.** The published
  `@anthropic-ai/mcpb` (2.1.2, 2025-12-04) appends the PKCS#7 blob past the zip
  end-of-central-directory record but leaves that record's comment-length field at 0.
  Lenient readers (Python `zipfile`, `unzip`) skip the orphaned bytes; Claude Desktop uses
  a strict reader and refuses the file with `Invalid comment length`. Reproduced with both
  `--self-signed` and a real CA-issued chain. Upstream issue
  [#278](https://github.com/modelcontextprotocol/mcpb/issues/278), fixed by
  [PR #204](https://github.com/modelcontextprotocol/mcpb/pull/204) (merged 2026-03-18) and
  never released — npm has had no publish since 2025-12-04.

  `mcpb_cli.sh` therefore builds the CLI from a pinned upstream commit that carries the
  fix, and `build.sh` and `sign.py` both use that. Bump the SHA deliberately; never point
  it at a moving ref.

  The pin covers the dependencies too. Upstream vendors its own yarn release at
  `.yarn/releases/` and commits `yarn.lock`, so installing with that binary and
  `--immutable` fixes all 605 transitive packages to the same commit. Do not substitute
  `npm install` — it ignores a yarn lockfile and resolves the tree fresh at build time,
  which is the code that then handles the signing certificate and private key.

  ```bash
  uv run --no-project python packaging/mcpb/sign.py <bundle.mcpb> cert.pem key.pem [chain.pem]
  uv run --no-project python packaging/mcpb/verify.py <bundle.mcpb>
  ```

  Two traps worth knowing. The built CLI still reports `--version` 2.1.2, because upstream
  never bumped main, so the version string tells you nothing about whether the fix is
  present — `sign.py` checks the archive structure instead. And `tsc` reports one
  pre-existing type error upstream while still emitting; `mcpb_cli.sh` tolerates the exit
  code and then requires the binary to exist and run.

- **Signing is not wired into CI, and buying a certificate would probably not fix that.**
  Two independent obstacles, so establish the need before spending anything.

  The tooling wants a private key as a file: `sign.py` passes `--cert cert.pem --key
  key.pem` through to the CLI. Certificates issued since 2023-06-01 cannot supply one.
  CA/Browser Forum rules require the key for a code-signing certificate to be generated on,
  and never leave, hardware certified to FIPS 140 Level 2 or Common Criteria EAL 4+ — a
  shipped token or an HSM, non-exportable — and CAs withdrew the flow that produced an
  installable key file. Consuming such a certificate needs HSM or cloud-signing support the
  CLI does not have, in a repository that has not published to npm since 2025-12-04.

  Nothing checks the result either. `mcpb verify` cannot confirm any signature (see below),
  and Claude Desktop carries exactly one user-facing string about extension signatures —
  *"This extension isn't signed. Ask your administrator to allow it or install a signed
  version."* Its neighbours in the same translation file are all organisation policy:
  extensions disabled because they are not allowed in the current workspace, extensions
  blocked by a security blocklist. An ordinary install shows no signature state at all, and
  the unsigned bundle installs without a warning.

  So signing looks like it matters only for distribution into an enterprise-managed
  workspace that enforces signed extensions. If that becomes a requirement, test
  `mcpb sign --self-signed` against the policy first — it is free and yields status
  `self-signed` rather than `unsigned`. Only if the policy rejects that is a purchased
  certificate worth pricing, and the key-storage problem above has to be solved before one
  is useful here. Wiring it up afterwards means repository secrets holding whatever the
  chosen mechanism needs, plus a signing step in `bundle.yml` before the upload.

- **`mcpb verify` cannot confirm any signature.** It calls node-forge's
  `PkcsSignedData.verify()`, which node-forge has never implemented — it always throws, and
  the catch-all maps that to "not signed". Affects every signature, self-signed or not.
  Upstream issues [#277](https://github.com/modelcontextprotocol/mcpb/issues/277) and
  [#21](https://github.com/modelcontextprotocol/mcpb/issues/21) (open since 2025-06-28).
  Never gate CI on `mcpb verify`; `verify.py` is the structural check that actually works.
- **The host pins an old `uv`, and that is the one users without `uv` will get.** When
  discovery finds nothing it downloads a fixed version — 0.9.7, dated 2025-10-30 — not the
  latest. Anyone who already has `uv` runs the bundle on theirs instead, so the two
  populations build the venv with different `uv` versions, and the older one is the branch
  nobody developing this will hit by accident. The shipped `uv.lock` is `version = 1`,
  `revision = 3`, which 0.9.7 reads. **Re-check that after any change that regenerates the
  lock**: a newer lock format would fail only for users without `uv`, who are the least
  equipped to work out why.

- **Claude Desktop's `uv` provisioning is confirmed on both branches; other hosts are not.**
  Verified by two real installs on macOS on 2026-07-31 (app build 2026-07-24). With a system
  `uv` present: `[UV Discovery] ✓ Found system UV: /opt/homebrew/bin/uv`. With `uv` unlinked
  and no cached copy: `System UV not found, downloading bundled version...`, `Downloading UV
  0.9.7 for darwin-arm64...`, `✓ Download verified successfully`, then `uv sync` and a normal
  launch. Both produced a `.venv` on a `uv`-managed CPython 3.13.13 rather than a system
  interpreter, so the no-Python-prerequisite claim is observed rather than inferred. Two
  harmless artifacts to expect in the log: quarantine removal fails with `No such xattr:
  com.apple.quarantine`, because an HTTP download is never quarantined; and every later
  launch re-runs discovery, fails, and reports `✓ Using cached bundled UV`. Claude Code and
  MCP for Windows also accept `.mcpb`, but whether they provision `uv` this way is unverified
  — `compatibility.runtimes.python` is what surfaces the requirement if one of them does not.
