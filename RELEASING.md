# Releasing `delta-exchange-mcp`

Runbook for cutting a new version to PyPI and GitHub. Aimed at maintainers with PyPI write access on the `delta-exchange-mcp` project and write access to this repo.

This covers: bumping the version, publishing to PyPI, tagging, drafting the GitHub release. It does **not** cover CHANGELOG bookkeeping (GitHub release notes are the chronicle for now) or automatic version bumping.

## What gets versioned

The `version` field in `pyproject.toml` is the single source of truth. The `delta-exchange-mcp/<version>` string in `User-Agent` and `Source` request headers is derived from it (see `src/delta_exchange_mcp/client.py`, which reads `importlib.metadata.version("delta-exchange-mcp")`). Nothing else in the *code* needs to change.

Two committed files do, though, and both are checked in CI:

- **`uv.lock`** pins the workspace's own version. `uv sync` refreshes it.
- **`packaging/mcpb/manifest.json`** is generated but committed, so the shipped bundle contract is reviewable in a diff. It carries the version, and the Bundle workflow runs on any change to `pyproject.toml` and then enforces `git diff --exit-code -- packaging/mcpb/manifest.json`. **A version bump with a stale manifest turns the Bundle check red.** Regenerate it in step 3 below.

SemVer while in Beta:

- Patch (`0.1.0` → `0.1.1`) for bug fixes and doc-only releases when you want the rendered PyPI page to refresh.
- Minor (`0.1.x` → `0.2.0`) for new tools, env variables, or behavior changes.
- Major bumps are deferred until exiting Beta.

## One-time prerequisites

1. PyPI account with maintainer access on `delta-exchange-mcp`: https://pypi.org/manage/project/delta-exchange-mcp/
2. A **project-scoped** PyPI API token created under *Manage project → Settings → Create a token*. Store it locally as `UV_PUBLISH_TOKEN` (e.g. in your shell rc or 1Password). Never check it in. Do not reuse account-scoped tokens past the initial-claim release.
3. `gh` CLI logged in (`gh auth status` is green).
4. Clean working tree on an up-to-date `main` (`git status` empty, `git pull --ff-only`).
5. **Node 22+**, for regenerating the bundle manifest. `packaging/mcpb/build.sh` compiles the `mcpb` CLI from a pinned upstream commit rather than installing the npm release, because the published one signs bundles Claude Desktop refuses. First run takes a few minutes; after that it is cached.

## Cut a release

```bash
# 1. Pick the new version
NEW_VERSION=0.1.1

# 2. Bump pyproject.toml
sed -i '' "s/^version = \".*\"/version = \"$NEW_VERSION\"/" pyproject.toml
git diff pyproject.toml          # sanity check the diff

# 3. Run tests + lint, and regenerate the bundle
uv sync                          # regenerates uv.lock with the new workspace version
uv run pytest
uv run ruff check src tests scripts packaging

# The manifest carries the version and is committed, so it goes stale on every bump. This
# rebuilds and verifies the whole bundle; the only tracked file it changes is manifest.json.
bash packaging/mcpb/build.sh
git diff --stat packaging/mcpb/manifest.json   # expect the version field, and tools if they moved

# 4. Commit + tag
git add pyproject.toml uv.lock packaging/mcpb/manifest.json
git commit -m "Release v$NEW_VERSION"
git tag -a "v$NEW_VERSION" -m "v$NEW_VERSION"

# 5. Build + publish to PyPI
rm -rf dist/
uv build
uv publish                       # reads UV_PUBLISH_TOKEN from env

# 6. Push commit + tag
git push origin main "v$NEW_VERSION"

# 7. Create the GitHub release (notes template below)
gh release create "v$NEW_VERSION" --title "v$NEW_VERSION" --notes-file /tmp/release-notes.md
```

If `uv publish` fails after step 4 (token missing, network glitch), drop the tag (`git tag -d "v$NEW_VERSION"`), fix the issue, and start from step 4 again. PyPI rejects re-uploading the same version, so don't re-run step 5 with the same `NEW_VERSION` after a partial success.

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

## Future: automate with Trusted Publishers

The manual procedure is intentional for the Beta phase. The planned next step is **PyPI Trusted Publishers** (GitHub Actions OIDC, no stored tokens):

1. Add a Pending Publisher on PyPI pointing at `delta-exchange/delta-exchange-mcp`, workflow `release.yml`, environment `pypi`.
2. Create a `pypi` environment in GitHub repo settings (optionally with manual-approval protection).
3. Add `.github/workflows/release.yml` that triggers on `v*` tag pushes, runs `uv build`, and uses `pypa/gh-action-pypi-publish@release/v1`.

Once wired, steps 5–7 of the manual procedure collapse into a single `git push origin "v$NEW_VERSION"` and CI handles the upload + release creation. Tracked as a follow-up; do not assume it's in place when running this runbook.
