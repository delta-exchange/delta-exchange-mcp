"""MCP App that opens the external Manage Connection page."""

from mcp.server.apps import APP_MIME_TYPE

VIEW_URI = "ui://delta-exchange/manage-connection.html"
VIEW_MIME = APP_MIME_TYPE

VIEW_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Manage Delta Exchange connection</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; padding: 1em; }
  button { font: inherit; min-height: 2.75em; padding: .5em 1em; }
  p { margin: 0 0 .75em; }
</style>
</head>
<body>
<p id="status">Opening the secure connection page…</p>
<button id="open" type="button" hidden>Open connection page</button>
<script>
(() => {
  "use strict";
  const status = document.getElementById("status");
  const button = document.getElementById("open");
  const pending = new Map();
  let nextId = 1;
  let manageUrl = "";

  function post(message) { window.parent.postMessage(message, "*"); }
  function request(method, params) {
    const id = nextId++;
    post({ jsonrpc: "2.0", id, method, params: params || {} });
    return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
  }
  function open() {
    if (!manageUrl) return;
    request("ui/open-link", { url: manageUrl }).catch(() => {
      status.textContent = "This client could not open the connection page.";
      button.hidden = false;
    });
  }
  function capture(result) {
    const ui = result && result._meta && result._meta.ui;
    if (!ui || !ui.manageUrl) return;
    manageUrl = ui.manageUrl;
    button.hidden = false;
    open();
  }

  window.addEventListener("message", event => {
    if (event.source !== window.parent) return;
    const message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    if (message.id !== undefined && message.method === undefined) {
      const callback = pending.get(message.id);
      if (!callback) return;
      pending.delete(message.id);
      if (message.error) callback.reject(new Error(message.error.message));
      else callback.resolve(message.result);
      return;
    }
    if (message.method === "ui/notifications/tool-result") capture(message.params);
  });

  button.addEventListener("click", open);
  request("ui/initialize", {
    appCapabilities: {},
    appInfo: { name: "Delta Exchange connection", version: "1" },
    protocolVersion: "2026-07-28",
  }).then(result => {
    post({ jsonrpc: "2.0", method: "ui/notifications/initialized" });
    capture(result && result.toolResult);
  }).catch(() => {
    status.textContent = "This client could not initialize the connection view.";
  });
})();
</script>
</body>
</html>
"""
