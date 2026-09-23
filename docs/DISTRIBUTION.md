# Distribution checklist

Where `delta-exchange-mcp` is listed, beyond PyPI itself. One entry (the official registry)
is automatic; the rest are one-time manual submissions — check them off, don't repeat them
per release.

## Automatic

- **Official MCP registry** (`registry.modelcontextprotocol.io`) — `.github/workflows/release.yml`
  publishes `server.json` on every tag push via `mcp-publisher`. Nothing to do here per release.
  Verify a listing: https://registry.modelcontextprotocol.io/v0/servers?search=delta-exchange-mcp

## Manual, one-time

- [ ] **PulseMCP** (https://www.pulsemcp.com) — ingests from the official registry automatically.
  No submission form; confirm it picked up the listing a few days after the first
  `server.json` publish: https://www.pulsemcp.com/servers?q=delta-exchange
- [ ] **mcp.so** (https://mcp.so/submit) — submit the GitHub repo URL
  (`https://github.com/delta-exchange/delta-exchange-mcp`) through their submission form.
- [ ] **punkpeye/awesome-mcp-servers** — open a PR against
  https://github.com/punkpeye/awesome-mcp-servers adding one line to the
  `Finance & Fintech` section (`### 💰 Finance & Fintech`), alphabetized by repo owner:

  ```markdown
  - [delta-exchange/delta-exchange-mcp](https://github.com/delta-exchange/delta-exchange-mcp) 🐍 ☁️ 🍎 🪟 🐧 - Official MCP server for Delta Exchange India: live market data for everyone, plus your own account reads and trading with an API key.
  ```

- [ ] **Cursor directory** (https://cursor.directory/mcp) — submit via their "Submit MCP" form
  with the GitHub repo URL and the PyPI package name.
- [ ] **Anthropic desktop extensions directory** — the `.mcpb` bundle already exists at
  `packaging/mcpb/` and is attached to every GitHub release by `bundle.yml`. Submit it per
  Anthropic's desktop-extensions directory process (see the `manifest.json` in that folder for
  the metadata reviewers will check against).

## Notes

- Keep the `<!-- mcp-name: io.github.delta-exchange/delta-exchange-mcp -->` marker in
  `README.md` intact — the official registry re-checks it on every publish to verify PyPI
  package ownership (`tests/test_server_json.py` checks it locally first).
- Re-run the manual submissions only if the server is renamed or moves to a different repo;
  a normal version release does not require touching any of them.
