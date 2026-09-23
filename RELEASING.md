# Releasing `delta-exchange-mcp`

Runbook for cutting a new version to PyPI and GitHub. Aimed at maintainers with PyPI write access on the `delta-exchange-mcp` project and write access to this repo.

This covers: bumping the version, publishing to PyPI, tagging, drafting the GitHub release. It does **not** cover automatic version bumping. Notable changes go in `CHANGELOG.md`; the GitHub release notes repeat them.

## What gets versioned

The `version` field in `pyproject.toml` is the single source of truth. The `delta-exchange-mcp/<version>` string in `User-Agent` and `Source` request headers is derived from it (see `src/delta_exchange_mcp/client.py`, which reads `importlib.metadata.version("delta-exchange-mcp")`). Nothing else in the *code* needs to change.

Three committed files do, though, and all three are checked in CI:

- **`uv.lock`** pins the workspace's own version. `uv sync` refreshes it.
- **`packaging/mcpb/manifest.json`** is generated but committed, so the shipped bundle contract is reviewable in a diff. It carries the version, and the Bundle workflow runs on any change to `pyproject.toml` and then enforces `git diff --exit-code -- packaging/mcpb/manifest.json`. **A version bump with a stale manifest turns the Bundle check red.** Regenerate it in step 3 below.
- **`server.json`** (repo root) is the MCP registry's own manifest — its `version` and `packages[0].version` must both match `pyproject.toml`. `.github/workflows/release.yml`'s `guard` job fails the whole release the moment they don't, on every tag push. There is no regeneration script for it; bump it by hand alongside `pyproject.toml` in step 2 below. `tests/test_server_json.py` catches a stale copy locally, before it ever reaches a tag push.

SemVer while in Beta:

- Patch (`0.1.0` → `0.1.1`) for bug fixes and doc-only releases when you want the rendered PyPI page to refresh.
- Minor (`0.1.x` → `0.2.0`) for new tools, env variables, or behavior changes.
- Major bumps are deferred until exiting Beta.

## One-time prerequisites

Publishing itself runs in CI on `.github/workflows/release.yml` via trusted publishing — GitHub
Actions OIDC to both PyPI and the MCP registry, no stored tokens. Two things have to be wired up
once, by someone with admin on the `delta-exchange` PyPI project and the GitHub org, before the
first tag push will succeed:

1. **PyPI trusted publisher.** On https://pypi.org/manage/project/delta-exchange-mcp/settings/publishing/,
   add a GitHub publisher with:
   - Owner: `delta-exchange`
   - Repository: `delta-exchange-mcp`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
2. **`pypi` GitHub environment.** In the repo's *Settings → Environments*, create an environment
   named `pypi` (no secrets needed — OIDC carries the identity). Optionally add required
   reviewers or a deployment-branch rule restricting it to `main`/`v*` tags; see
   [the registry's own guidance on securing CI publish tokens](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/github-actions.mdx#securing-your-registry-token-in-ci)
   for why that matters even though this path has no long-lived token to leak.

Nothing equivalent is needed on the MCP registry side: `mcp-publisher login github-oidc` grants
`io.github.delta-exchange/*` to any workflow run in this org, checked at login time against the
repo's own OIDC token — there is no separate registration step.

For cutting a release by hand (day-to-day maintainer setup):

1. `gh` CLI logged in (`gh auth status` is green) — used for the GitHub release, not for publishing.
2. Clean working tree on an up-to-date `main` (`git status` empty, `git pull --ff-only`).
3. **Node 22+**, for regenerating the bundle manifest. `packaging/mcpb/build.sh` compiles the `mcpb` CLI from a pinned upstream commit rather than installing the npm release, because the published one signs bundles Claude Desktop refuses. First run takes a few minutes; after that it is cached.

A **project-scoped PyPI API token** (`UV_PUBLISH_TOKEN`, `uv publish`) is no longer part of the
normal flow. Keep one in a secret manager only as a manual break-glass fallback for publishing a
version by hand if the trusted-publishing path is itself broken — see
[Rolling back a bad release](#rolling-back-a-bad-release).

## Cut a release

### Run the authenticated testnet permission matrix

Before a release that changes account authorization, create two separate testnet keys.
Give one key Read Data permission and the other key Trading permission. Load these values
from the team's secret manager into the shell without putting them in command history:

- `DELTA_MCP_TESTNET_READ_DATA_API_KEY`
- `DELTA_MCP_TESTNET_READ_DATA_API_SECRET`
- `DELTA_MCP_TESTNET_TRADING_API_KEY`
- `DELTA_MCP_TESTNET_TRADING_API_SECRET`

Run the matrix from the repository root:

```bash
uv run python scripts/permission_matrix.py
```

The script calls only these authenticated testnet GET endpoints:

- `/users/trading_preferences`
- `/positions/margined`
- `/orders`
- `/wallet/balances`

Exit 0 means every cell produced either `allowed` or `permission_denied`. It means the
run completed, not that every permission works. Claim Read Data compatibility only when
all four `read_data` cells say `allowed`. Exit 1 means a request or response failed. Exit
2 means one or both credential pairs were missing, so the release gate did not run.

The output contains no response bodies or credential data. Remove the four variables
from the shell after the run.

```bash
# 1. Pick the new version
NEW_VERSION=0.1.1

# 2. Bump pyproject.toml and server.json together — release.yml's guard job fails the
#    whole release if the tag, pyproject.toml, and server.json don't all agree.
sed -i '' "s/^version = \".*\"/version = \"$NEW_VERSION\"/" pyproject.toml
jq --arg v "$NEW_VERSION" '.version = $v | .packages[0].version = $v' server.json > server.json.tmp \
  && mv server.json.tmp server.json
git diff pyproject.toml server.json   # sanity check both diffs

# 3. Run tests + lint, and regenerate the bundle
uv sync                          # regenerates uv.lock with the new workspace version
uv run pytest
uv run ruff check src tests scripts packaging

# The manifest carries the version and is committed, so it goes stale on every bump. This
# rebuilds and verifies the whole bundle; the only tracked file it changes is manifest.json.
bash packaging/mcpb/build.sh
git diff --stat packaging/mcpb/manifest.json   # expect the version field, and tools if they moved

# 4. Commit + tag + push
git add pyproject.toml uv.lock server.json packaging/mcpb/manifest.json
git commit -m "Release v$NEW_VERSION"
git tag -a "v$NEW_VERSION" -m "v$NEW_VERSION"
git push origin main "v$NEW_VERSION"

# 5. Watch release.yml: it re-runs tests, builds, publishes to PyPI via trusted
#    publishing (OIDC, no token), then publishes server.json to the MCP registry the
#    same way. Wait for it to go green before announcing anything.
gh run watch --exit-status "$(gh run list --workflow=release.yml --branch=main --limit=1 --json databaseId -q '.[0].databaseId')"

# 6. Create the GitHub release (notes template below) — this is still a manual step,
#    and it's what triggers the Bundle workflow's `attach` job (release: published).
gh release create "v$NEW_VERSION" --title "v$NEW_VERSION" --notes-file /tmp/release-notes.md
```

If `release.yml` fails after step 4 (guard mismatch, a flaky test, a PyPI trusted-publisher
misconfiguration), fix the issue and re-tag: `git tag -d "v$NEW_VERSION" && git push origin :refs/tags/"v$NEW_VERSION"`,
then repeat from step 4. PyPI rejects re-uploading the same version, so if `publish-pypi` already
succeeded before a later job failed, bump to a new patch version instead of retrying the same tag.

## Release-notes template

Paste into `/tmp/release-notes.md` before step 7:

```markdown
## Added
- ...

## Fixed
- ...

## Changed
- ...

## Install

\`\`\`bash
uvx "delta-exchange-mcp==<NEW_VERSION>"
\`\`\`
```

Drop sections that don't apply. Keep bullets terse, link to PRs / issues by number.

## Pre-releases (optional)

For an experimental cut you don't want `uvx delta-exchange-mcp` to resolve to, use a SemVer pre-release suffix (`0.2.0a1`, `0.2.0rc1`) and add `--prerelease` to `gh release create`. Both `uv` and `pip` skip pre-releases by default, so end users on the floating install path stay on the last stable version.

## Post-release verification

```bash
# 1. PyPI shows the new version
curl -s https://pypi.org/pypi/delta-exchange-mcp/json | jq '.info.version'

# 2. Fresh install resolves to the new version (clears uvx cache)
uvx --refresh delta-exchange-mcp --help

# 3. Smoke test public tools through the freshly-spawned server
bash scripts/inspect.sh --cli --method tools/list
bash scripts/inspect.sh --cli --method tools/call --tool-name get_ticker --tool-arg symbol=BTCUSD

# 4. The bundle actually attached. The Bundle workflow's attach job fires on
#    `release: published` only, so a draft release gets no asset — and the README's
#    Claude Desktop install path is only useful once one is there.
gh release view "v$NEW_VERSION" --json assets -q '.assets[].name'
curl -sIL -o /dev/null -w '%{http_code}\n' \
  "https://github.com/delta-exchange/delta-exchange-mcp/releases/latest/download/delta-exchange-mcp.mcpb"
```

Expect two asset names — `delta-exchange-mcp-<version>.mcpb` and the unversioned
`delta-exchange-mcp.mcpb` alias — and `200` from the curl. The alias exists so that
`/releases/latest/download/` has a name that does not change between releases, and the README
badge points directly at it. The Bundle workflow's `attach` job uploads the alias only after
the release is published, so the direct link can briefly return 404 while that job runs. Wait
for the attach job and this curl to succeed before announcing the release.

## Rolling back a bad release

PyPI does **not** allow overwriting a published version. If a release is broken:

1. **Yank** the version on PyPI: *Manage project → Releases → yank*. Yanking keeps the version installable for users with an exact pin but hides it from floating resolution (`uvx delta-exchange-mcp` will skip it).
2. Cut a new patch version with the fix following the procedure above.
3. Leave the original GitHub release and tag in place for history. Delete them only if you also yanked the corresponding PyPI release.

## MCP registry listing

`server.json` at the repo root is what `mcp-publisher publish` sends to
`registry.modelcontextprotocol.io`; `.github/workflows/release.yml`'s `publish-mcp-registry` job
runs it automatically on every tag push, after the PyPI publish succeeds. The registry entry is
what PulseMCP, the Cursor directory, and other aggregators read from — see `docs/DISTRIBUTION.md`
for the listings that still have to be done by hand.

The PyPI ownership check for this server name reads the `<!-- mcp-name: ... -->` marker in
`README.md` (PyPI renders it as the package's long description), so that marker has to keep
matching `server.json`'s `name` field exactly, boundary and all — see
`tests/test_server_json.py`.
