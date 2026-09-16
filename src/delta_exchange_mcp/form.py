"""Render the browser-only Delta connection page.

The page sends credentials directly to the loopback HTTP service. It does not expose an
MCP tool-call transport, so credentials never become model-visible tool arguments.
"""

import base64
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from delta_exchange_mcp.config import DASHBOARDS, DEFAULT_ENV

_FONTS = Path(__file__).parent / "fonts"
# Aileron is Delta's typeface (CC0; see fonts/LICENSE). The page's CSP is
# default-src 'none', so the faces travel in the document rather than over the
# network: the connection page renders on brand with no request of its own.
_FACES = (("Aileron-Regular", 400), ("Aileron-SemiBold", 600), ("Aileron-Bold", 700))


@lru_cache(maxsize=1)
def font_faces() -> str:
    """Return @font-face rules carrying the brand faces as data URIs."""
    rules = []
    for name, weight in _FACES:
        encoded = base64.b64encode((_FONTS / f"{name}.woff2").read_bytes()).decode()
        rules.append(
            "  @font-face { font-family: Aileron; font-style: normal; "
            f"font-weight: {weight}; font-display: swap; "
            f'src: url("data:font/woff2;base64,{encoded}") format("woff2"); }}'
        )
    return "\n".join(rules)


ENVIRONMENTS = [
    {"value": "india_prod", "label": "Real account", "site": "delta.exchange"},
    {
        "value": "india_testnet",
        "label": "Practice account",
        "site": "demo.delta.exchange",
    },
    {
        "value": "india_devnet",
        "label": "Development account",
        "site": "process credentials only",
        "hidden": True,
    },
]


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Manage Delta Exchange connection</title>
<style__NONCE_ATTR__>
__FONT_FACES__
  :root {
    /* Delta Exchange design tokens, read from the live site's brand-india and
       main scales. Each pair is light-dark(<light>, <dark>). */
    color-scheme: light dark;
    --brand-strong: #fe6c02;
    --brand-strong-hover: #e76202;
    --brand-text: light-dark(#e76202, #fe8935);
    --brand-muted: light-dark(#fff0e6, #2f231b);
    --brand-muted-line: light-dark(#ffbb8b, #8c3b01);
    --on-brand: #ffffff;
    --positive: light-dark(#00996b, #00a876);
    --negative: light-dark(#dc4e4e, #eb5454);
    --ink: light-dark(#121214, #e1e1e2);
    --muted: #8e9298;
    --line: light-dark(#d7dde7, #353845);
    --field: light-dark(#ebecf0, #111114);
    --control: light-dark(#e6e9ef, #2d303a);
    --surface: light-dark(#f3f4f6, #18191e);
    --surface-raised: light-dark(#ffffff, #22242c);
    --radius: .25rem;
    --radius-card: .5rem;
    --gap: 1rem;
    --gap-tight: .45rem;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--surface); color: var(--ink); }
  body { font-family: Aileron, ui-sans-serif, system-ui, -apple-system, sans-serif; line-height: 1.5; }
  main { max-width: 46rem; margin: 0 auto; padding: 1.25rem; }
  header { display: flex; align-items: center; gap: .7rem; margin-bottom: var(--gap); }
  .mark { width: 2rem; height: 2rem; flex: none; }
  h1, h2 { line-height: 1.2; margin: 0; }
  h1 { font-size: 1.35rem; }
  h2 { font-size: 1.05rem; }
  p { margin: 0; }
  .sub, .note { color: var(--muted); }
  .sub { margin-bottom: var(--gap); }
  section {
    border: 1px solid var(--line); border-radius: var(--radius-card);
    background: var(--surface-raised);
    padding: var(--gap); margin-bottom: var(--gap);
  }
  section > * + * { margin-top: var(--gap-tight); }
  fieldset { border: 0; padding: 0; margin: 0 0 var(--gap); }
  legend, .label { display: block; font-weight: 600; margin-bottom: var(--gap-tight); }
  .choice { display: flex; align-items: baseline; gap: .45rem; padding: .25rem 0; }
  .site { color: var(--muted); }
  .field { margin-top: .75rem; }
  input[type=password], input[type=text] {
    width: 100%; min-height: 2.75rem; padding: .55rem .7rem;
    color: var(--ink); background: var(--field);
    border: 1px solid var(--line); border-radius: var(--radius); font: inherit;
  }
  input[type=radio], input[type=checkbox] { accent-color: var(--brand-strong); }
  .row { display: flex; flex-wrap: wrap; gap: .65rem; align-items: center; margin-top: .8rem; }
  button, .button {
    display: inline-flex; align-items: center; justify-content: center;
    min-height: 2.75rem; padding: .55rem .9rem; border-radius: var(--radius);
    border: 0; background: var(--brand-strong); color: var(--on-brand);
    font: inherit; font-weight: 600; text-decoration: none; cursor: pointer;
  }
  button:hover, .button:hover { background: var(--brand-strong-hover); }
  button.secondary, .button.secondary {
    color: var(--ink); background: var(--control); border: 1px solid var(--line);
  }
  button.secondary:hover, .button.secondary:hover { border-color: var(--brand-strong); }
  button.danger { color: var(--negative); background: transparent; border: 1px solid currentcolor; }
  button[disabled], .button[aria-disabled="true"] { cursor: not-allowed; opacity: .55; }
  :focus-visible { outline: 2px solid var(--brand-strong); outline-offset: 2px; }
  #notice { min-height: 1.5rem; margin-bottom: var(--gap); }
  #notice.good { color: var(--positive); }
  #notice.bad { color: var(--negative); }
  #prod-ack[hidden], [hidden] { display: none !important; }
  .environment-status { display: grid; grid-template-columns: max-content 1fr; gap: .2rem .65rem; }
  .environment-status dt { color: var(--muted); }
  .environment-status dd { margin: 0; }
  @media (max-width: 34rem) {
    main { padding: .85rem; }
    .row button { width: 100%; }
  }
</style>
</head>
<body>
<main>
  <header>
    <svg class="mark" viewBox="0 0 30 30" aria-hidden="true">
      <defs>
        <linearGradient id="delta-mark-warm" x1="32.3212" y1="8.56271" x2="20.8051" y2="-1.10809"
                        gradientUnits="userSpaceOnUse">
          <stop stop-color="#E96C04"/>
          <stop offset="1" stop-color="#FF9300"/>
        </linearGradient>
        <linearGradient id="delta-mark-cool" x1="22.8785" y1="10.7865" x2="11.9291" y2="20.6491"
                        gradientUnits="userSpaceOnUse">
          <stop stop-color="#168016"/>
          <stop offset="1" stop-color="#2CB72C"/>
        </linearGradient>
      </defs>
      <path fill="url(#delta-mark-warm)" d="M10.1209 10.0001L19.8791 15.0001L29.6372 10.0001L10.1209 0V10.0001Z"/>
      <path fill="url(#delta-mark-cool)" d="M10.1212 19.9999V30L29.6375 19.9999L19.8793 14.9999L10.1212 19.9999Z"/>
      <path fill="#2CB72C" d="M29.6381 20.0002V10.0001L19.8799 15.0002L29.6381 20.0002Z"/>
      <path fill="#FF9300" d="M10.12 10.0001V20.0002L0.361877 15.0002L10.12 10.0001Z"/>
    </svg>
    <h1>Manage Delta Exchange connection</h1>
  </header>
  <p class="sub">Your key and secret go directly from this page to the local MCP service.
  Do not paste them into a chat.</p>
  <div id="notice" role="status" aria-live="polite"></div>

  <section aria-labelledby="environment-title">
    <h2 id="environment-title">Environment</h2>
    <fieldset id="envs">
      <legend class="note">Choose the account that this MCP service uses.</legend>
    </fieldset>
    <dl class="environment-status">
      <dt>Connection</dt><dd id="connection-state">Checking…</dd>
      <dt>Validation</dt><dd id="validation-state">—</dd>
      <dt>Account</dt><dd id="account-state">—</dd>
      <dt>Storage</dt><dd id="storage-state">—</dd>
    </dl>
    <div class="row">
      <button id="activate" class="secondary" type="button">Use this environment</button>
    </div>
  </section>

  <section aria-labelledby="credentials-title">
    <h2 id="credentials-title">API credentials</h2>
    <p class="note">Connect for the first time, or enter a new pair to rotate the current pair.</p>
    <p id="reconnect-note" class="note" role="status" hidden>Reconnect once after this update.
    The MCP cannot verify who owns the old OS credential record, so it leaves that record
    unchanged. Enter your key and secret here, then approve trading again if needed.</p>
    <div class="field">
      <label class="label" for="key">API key</label>
      <input id="key" type="password" autocomplete="off" autocapitalize="none"
             spellcheck="false" placeholder="Paste the API key">
    </div>
    <div class="field">
      <label class="label" for="secret">API secret</label>
      <input id="secret" type="password" autocomplete="off" autocapitalize="none"
             spellcheck="false" placeholder="Paste the API secret">
    </div>
    <div class="row">
      <label><input id="show" type="checkbox"> Show what I typed</label>
      <a id="dashboard" class="button secondary" target="_blank"
         rel="noopener noreferrer">Open the API key page</a>
    </div>
    <div class="row">
      <button id="connect" type="button">Connect or rotate</button>
      <button id="disconnect" class="danger" type="button">Disconnect</button>
    </div>
  </section>

  <section aria-labelledby="trading-title">
    <h2 id="trading-title">Trading consent</h2>
    <p id="trading-state" class="note">Trading is off.</p>
    <label id="prod-ack" hidden>
      <input id="acknowledge" type="checkbox">
      I understand that this enables real orders on the production account.
    </label>
    <div class="row">
      <button id="enable-trading" type="button">Enable trading</button>
      <button id="disable-trading" class="danger" type="button">Disable trading</button>
    </div>
  </section>
</main>
<script__NONCE_ATTR__>
(function () {
  "use strict";
  var CONFIG = __CONFIG__;
  var csrfToken = CONFIG.csrf_token || "";
  var revision = CONFIG.revision === undefined ? 0 : CONFIG.revision;
  var current = null;
  var busy = false;
  var complete = false;

  var envs = document.getElementById("envs");
  var environmentLabels = {};
  var key = document.getElementById("key");
  var secret = document.getElementById("secret");
  var show = document.getElementById("show");
  var notice = document.getElementById("notice");
  var acknowledge = document.getElementById("acknowledge");
  var prodAck = document.getElementById("prod-ack");

  CONFIG.environments.forEach(function (env, index) {
    var label = document.createElement("label");
    label.className = "choice";
    label.hidden = env.hidden === true;
    var radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "environment";
    radio.value = env.value;
    radio.checked = env.value === CONFIG.default_environment ||
      (index === 0 && !CONFIG.default_environment);
    label.appendChild(radio);
    label.appendChild(document.createTextNode(env.label + " "));
    var site = document.createElement("span");
    site.className = "site";
    site.textContent = "— " + env.site;
    label.appendChild(site);
    envs.appendChild(label);
    environmentLabels[env.value] = label;
  });

  function selectedEnvironment() {
    var selected = envs.querySelector("input[name=environment]:checked");
    return selected ? selected.value : CONFIG.default_environment;
  }

  function selectEnvironment(environment) {
    var selected = envs.querySelector('input[value="' + environment + '"]');
    if (selected) {
      selected.checked = true;
      environmentLabels[environment].hidden = false;
    }
    syncAcknowledgement();
  }

  function syncAcknowledgement() {
    var production = selectedEnvironment() === "india_prod";
    prodAck.hidden = !production;
    acknowledge.checked = false;
  }

  function message(text, kind) {
    notice.textContent = text || "";
    notice.className = kind || "";
  }

  function setBusy(value) {
    busy = value;
    document.querySelectorAll("button").forEach(function (button) {
      button.disabled = value;
    });
  }

  function request(action, args) {
    return fetch(CONFIG.endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({
        action: action,
        arguments: args || {},
        csrf_token: csrfToken,
        expected_revision: revision
      })
    }).then(function (response) {
      var nextToken = response.headers.get("X-CSRF-Token");
      if (nextToken) csrfToken = nextToken;
      return response.json().then(function (body) {
        if (body.revision !== undefined) revision = body.revision;
        if (!response.ok || body.error) {
          throw new Error((body.error && body.error.message) || "The action failed.");
        }
        return {
          content: body.result && body.result.structuredContent || {},
          complete: body.complete === true
        };
      });
    });
  }

  function selectedStatus(status) {
    var environments = status.environments || {};
    return environments[selectedEnvironment()] || {};
  }

  function render(status, syncSelection) {
    current = status;
    if (syncSelection && status.environment) selectEnvironment(status.environment);
    var selected = selectedStatus(status);
    var selectedIsActive = selected.active === true;
    var credentialsLocked = busy || selected.browser_manageable !== true || selected.externally_managed;
    document.getElementById("connection-state").textContent = selected.connected ? "Connected" : "Not connected";
    document.getElementById("validation-state").textContent = selected.validation_state || "—";
    document.getElementById("account-state").textContent = selected.account_id || "—";
    document.getElementById("storage-state").textContent = selected.credential_source || "—";
    document.getElementById("reconnect-note").hidden = selected.reconnect_required !== true;
    var trading = selectedIsActive ? (status.trading || {}) : {};
    document.getElementById("trading-state").textContent = selectedIsActive
      ? (trading.enabled
        ? "Trading is enabled for " + (status.client_name || "this session") + "."
        : "Trading is off for " + (status.client_name || "this session") + ".")
      : "Use this environment before you enable trading.";
    document.getElementById("activate").disabled = busy || selectedIsActive;
    document.getElementById("disconnect").disabled = busy ||
      !selected.browser_manageable ||
      (!selected.connected && !selected.credential_metadata_present) || selected.externally_managed;
    key.disabled = credentialsLocked;
    secret.disabled = credentialsLocked;
    show.disabled = credentialsLocked;
    document.getElementById("connect").disabled = credentialsLocked;
    var dashboard = document.getElementById("dashboard");
    var dashboardUrl = CONFIG.dashboards[selectedEnvironment()];
    if (dashboardUrl && !busy) {
      dashboard.setAttribute("href", dashboardUrl);
      dashboard.removeAttribute("aria-disabled");
    } else {
      dashboard.removeAttribute("href");
      dashboard.setAttribute("aria-disabled", "true");
    }
    document.getElementById("enable-trading").disabled = busy || !selectedIsActive || !selected.connected || trading.enabled;
    document.getElementById("disable-trading").disabled = busy || !selectedIsActive || !trading.enabled;
  }

  function refresh() {
    return request("status", {}).then(function (response) { render(response.content, true); });
  }

  function run(action, args, success) {
    if (busy || complete) return;
    setBusy(true);
    message("");
    request(action, args).then(function (response) {
      var result = response.content;
      key.value = "";
      secret.value = "";
      show.checked = false;
      key.type = secret.type = "password";
      if (response.complete) {
        complete = true;
        render(result.connection, true);
        prodAck.hidden = true;
        document.querySelectorAll("input, button").forEach(function (control) {
          control.disabled = true;
        });
        message(result.message + " Return to your MCP client and retry the request. " +
          "To make more changes, open Manage Connection again.", "good");
        return;
      }
      message(result.message || success, result.status === "rejected" ? "bad" : "good");
      return refresh();
    }).catch(function (error) {
      message(error.message, "bad");
    }).finally(function () {
      if (complete) return;
      setBusy(false);
      if (current) render(current, false);
    });
  }

  envs.addEventListener("change", function () {
    syncAcknowledgement();
    if (current) render(current, false);
  });
  show.addEventListener("change", function () {
    var type = show.checked ? "text" : "password";
    key.type = secret.type = type;
  });
  document.getElementById("activate").addEventListener("click", function () {
    run("credentials", { operation: "activate", environment: selectedEnvironment() }, "Environment changed.");
  });
  document.getElementById("connect").addEventListener("click", function () {
    if (!key.value.trim() || !secret.value.trim()) {
      message("Enter both the API key and the API secret.", "bad");
      return;
    }
    run("credentials", {
      operation: "replace",
      environment: selectedEnvironment(),
      api_key: key.value.trim(),
      api_secret: secret.value.trim()
    }, "Connection updated. You can now choose whether to enable trading.");
  });
  document.getElementById("disconnect").addEventListener("click", function () {
    run("credentials", { operation: "disconnect", environment: selectedEnvironment() }, "Disconnected.");
  });
  document.getElementById("enable-trading").addEventListener("click", function () {
    if (selectedEnvironment() === "india_prod" && !acknowledge.checked) {
      message("Confirm that this enables real production orders.", "bad");
      return;
    }
    run("consent", {
      enabled: true,
      environment: selectedEnvironment(),
      acknowledged: acknowledge.checked
    }, "Trading enabled.");
  });
  document.getElementById("disable-trading").addEventListener("click", function () {
    run("consent", { enabled: false, environment: selectedEnvironment() }, "Trading disabled.");
  });

  syncAcknowledgement();
  setBusy(true);
  refresh().catch(function (error) {
    message(error.message, "bad");
  }).finally(function () {
    setBusy(false);
    if (current) render(current, false);
  });
})();
</script>
</body>
</html>
"""


def _rendered(*, nonce: str = "", **extra: object) -> str:
    """Render one page with a secret-free configuration object."""
    settings: dict[str, object] = {
        "environments": ENVIRONMENTS,
        "dashboards": DASHBOARDS,
        "default_environment": DEFAULT_ENV,
    }
    settings.update(extra)
    nonce_attr = f' nonce="{nonce}"' if nonce else ""
    return (
        _TEMPLATE.replace("__CONFIG__", json.dumps(settings))
        .replace("__NONCE_ATTR__", nonce_attr)
        .replace("__FONT_FACES__", font_faces())
    )


def page_html(
    endpoint: str,
    *,
    csrf_token: str = "",
    revision: int | dict[str, int] = 0,
    nonce: str = "",
) -> str:
    """Render the page for a browser and its direct loopback action endpoint."""
    return _rendered(
        endpoint=endpoint,
        csrf_token=csrf_token,
        revision=revision,
        nonce=nonce,
    )


VIEW_HTML = page_html("")


def build_id() -> str:
    """Return a short build id for the browser page."""
    return hashlib.sha256(VIEW_HTML.encode()).hexdigest()[:10]
