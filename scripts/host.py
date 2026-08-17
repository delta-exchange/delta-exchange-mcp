"""Render the credential view inside a stand-in host, to see and measure it.

`CLAUDE.md` has described this harness for a while; this is the file it meant. It exists
because two things about the view can only be judged against a host, and neither shows up
in a unit test: the height it asks the host to draw, which the directory caps at 500px, and
how it looks once the host's own palette and typeface are applied over Delta's fallbacks.

Two chrome modes reproduce the two hosts that were measured. `tight` is Codex, which draws
a border and insets the frame by nothing. `host` is Claude Desktop, which draws a border
and does inset. The view pads itself for the first, which costs a little extra room inside
the second — the cheaper of the two failures, and the reason to be able to see both.

Everything is inlined into one file, so it opens from `file://` with no server. That is not
a stylistic choice: both hosts apply a content-security policy that blocks external
fetches, and a harness that loaded the view over HTTP would be testing something the real
hosts never do.

    uv run python scripts/host.py            # write it and print the path
    uv run python scripts/host.py --open     # and open it in the default browser

Append `?chrome=tight` or `?theme=dark` to the URL, or use the controls at the top.
"""

from __future__ import annotations

import argparse
import html
import json
import tempfile
import webbrowser
from pathlib import Path

from delta_exchange_mcp import form

# Representative of what a host actually sends. The names are the ones the view reads; the
# values only have to be plausible, because what is being checked is that the view follows
# them rather than its own fallbacks.
PALETTES = {
    "light": {
        "--color-text-primary": "#1f1f1f",
        "--color-text-secondary": "#5c5c5c",
        "--color-text-tertiary": "#8a8a8a",
        "--color-background-secondary": "#f5f4f2",
        "--color-border-primary": "#dcd9d4",
        "--font-text-sm-size": "13px",
        "--border-radius-md": "8px",
        "--border-width-regular": "1px",
        "--font-weight-medium": "500",
        "--font-weight-semibold": "600",
    },
    "dark": {
        "--color-text-primary": "#f2f2f2",
        "--color-text-secondary": "#a8a8a8",
        "--color-text-tertiary": "#767676",
        "--color-background-secondary": "#2a2a28",
        "--color-border-primary": "#3d3d3a",
        "--font-text-sm-size": "13px",
        "--border-radius-md": "8px",
        "--border-width-regular": "1px",
        "--font-weight-medium": "500",
        "--font-weight-semibold": "600",
    },
}

# What `get_connection_status` returns for someone with no key yet, which is the state the
# form is opened in. The view reads this to decide what the mode control should already say.
STATUS = {
    "environment": "india_prod",
    "credentials_configured": False,
    "account_tools_available": False,
    "mode": "read",
    "mode_after_restart": "read",
    "restart_required": False,
    "client_name": "host.py",
}

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Host harness — Delta Exchange credential view</title>
<style>
  :root { color-scheme: light dark; --ui: ui-sans-serif, system-ui, -apple-system, sans-serif; }
  body { margin: 0; font-family: var(--ui); background: Canvas; color: CanvasText; }
  header { display: flex; gap: 1.25rem; align-items: center; flex-wrap: wrap;
           padding: .75rem 1rem; border-bottom: 1px solid color-mix(in srgb, CanvasText 15%, Canvas); }
  header b { font-weight: 600; }
  label { font-size: .8125rem; display: inline-flex; gap: .35rem; align-items: center; }
  #readout { font-family: ui-monospace, Menlo, monospace; font-size: .8125rem; }
  #readout.over { color: #c00; font-weight: 600; }
  main { padding: 2rem 1rem; display: flex; justify-content: center; }
  /* A host draws the view in a column, not the full window. 400px is what both measured
     hosts land near, and the height depends on it. */
  #column { width: 400px; }
  /* The two chrome modes. `tight` draws the border and insets by nothing, which is what
     Codex does; `host` insets, which is what Claude Desktop does. */
  #frame { display: block; width: 100%; border: 0; }
  #box.tight { border: 1px solid color-mix(in srgb, CanvasText 20%, Canvas); border-radius: 8px; padding: 0; }
  #box.host  { border: 1px solid color-mix(in srgb, CanvasText 20%, Canvas); border-radius: 8px; padding: 12px; }
  #box.none  { border: 0; padding: 0; }
</style>
</head>
<body>
<header>
  <b>Host harness</b>
  <label>Chrome
    <select id="chrome">
      <option value="host">host — border and inset (Claude Desktop)</option>
      <option value="tight">tight — border, no inset (Codex)</option>
      <option value="none">none — no chrome at all</option>
    </select>
  </label>
  <label>Theme
    <select id="theme">
      <option value="light">light</option>
      <option value="dark">dark</option>
    </select>
  </label>
  <label><input type="checkbox" id="palette" checked> send the host palette</label>
  <span id="readout">height: waiting…</span>
</header>
<main><div id="column"><div id="box"><iframe id="frame" title="credential view" srcdoc="__VIEW__"></iframe></div></div></main>
<script>
  var PALETTES = __PALETTES__;
  var STATUS = __STATUS__;
  var CEILING = 500;

  var params = new URLSearchParams(location.search);
  var box = document.getElementById("box");
  var frame = document.getElementById("frame");
  var readout = document.getElementById("readout");
  var chromeEl = document.getElementById("chrome");
  var themeEl = document.getElementById("theme");
  var paletteEl = document.getElementById("palette");

  chromeEl.value = params.get("chrome") || "host";
  themeEl.value = params.get("theme") || "light";

  function hostContext() {
    return {
      theme: themeEl.value,
      styles: paletteEl.checked ? { variables: PALETTES[themeEl.value], css: { fonts: "" } } : {}
    };
  }

  function applyChrome() {
    box.className = chromeEl.value;
    document.documentElement.style.colorScheme = themeEl.value;
  }
  applyChrome();

  function reply(id, result) {
    frame.contentWindow.postMessage({ jsonrpc: "2.0", id: id, result: result }, "*");
  }

  window.addEventListener("message", function (event) {
    if (event.source !== frame.contentWindow) return;
    var msg = event.data;
    if (!msg || msg.jsonrpc !== "2.0") return;

    if (msg.method === "ui/initialize") {
      reply(msg.id, { hostContext: hostContext(), protocolVersion: msg.params.protocolVersion });
      return;
    }
    if (msg.method === "tools/call") {
      // Only the status read is answered. A save would need the server, and this harness
      // deliberately has none — nothing here should ever touch a real key.
      if (msg.params && msg.params.name === "get_connection_status") {
        reply(msg.id, { structuredContent: STATUS });
      } else {
        frame.contentWindow.postMessage({ jsonrpc: "2.0", id: msg.id,
          error: { code: -32601, message: "host harness has no server behind it" } }, "*");
      }
      return;
    }
    if (msg.method === "ui/open-link") {
      readout.textContent = "open-link: " + (msg.params && msg.params.url);
      if (msg.id !== undefined) reply(msg.id, {});
      return;
    }
    if (msg.method === "ui/notifications/size-changed") {
      var h = msg.params.height;
      frame.style.height = h + "px";
      readout.textContent = "height: " + h + "px  (ceiling " + CEILING + ")";
      readout.className = h > CEILING ? "over" : "";
      return;
    }
    if (msg.id !== undefined) {
      frame.contentWindow.postMessage({ jsonrpc: "2.0", id: msg.id,
        error: { code: -32601, message: "not implemented: " + msg.method } }, "*");
    }
  });

  // A theme change goes over the wire, because that notification is itself worth
  // exercising: it carries a method and no id, so a listener written only for replies and
  // host requests misses it, and the view then keeps the palette it started with.
  themeEl.addEventListener("change", function () {
    applyChrome();
    frame.contentWindow.postMessage({ jsonrpc: "2.0",
      method: "ui/notifications/host-context-changed", params: hostContext() }, "*");
  });

  // Turning the palette off reloads instead of notifying. It has to: the view applies a
  // host's variables with setProperty and never clears them, so a later context carrying
  // no palette cannot undo the first one. Notifying here would leave the old values in
  // place and report a "no palette" height that still had the palette in it.
  paletteEl.addEventListener("change", function () { frame.contentWindow.location.reload(); });
  chromeEl.addEventListener("change", applyChrome);
</script>
</body>
</html>
"""


def render() -> str:
    """One self-contained page with the current view inlined."""
    return (
        TEMPLATE.replace("__VIEW__", html.escape(form.VIEW_HTML, quote=True))
        .replace("__PALETTES__", json.dumps(PALETTES))
        .replace("__STATUS__", json.dumps(STATUS))
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, help="where to write it (default: a temp file)")
    parser.add_argument("--open", action="store_true", help="open it in the default browser")
    args = parser.parse_args()

    target = args.out or Path(tempfile.gettempdir()) / "delta-host-harness.html"
    target.write_text(render())
    print(f"{target}  (view build {form.build_id()})")
    if args.open:
        webbrowser.open(target.as_uri())


if __name__ == "__main__":
    main()
