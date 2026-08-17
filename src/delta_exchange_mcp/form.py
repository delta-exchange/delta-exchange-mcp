"""An in-chat form for entering an API key, so nobody has to open a terminal.

Every other way of setting a credential asks the user to leave the conversation: edit a
JSON file whose shape differs per client, or run a command. Marketing tried both and got
stuck on which file and whether a terminal was required, which is the problem this
solves. MCP Apps (SEP-1865) lets a server ship a small HTML view that the client renders
inline, so the key is typed into a field a few lines below the question that prompted it.

The point is not convenience. What is typed into the view is typed into an iframe the
model cannot read, and it reaches this process as the arguments of a view-initiated tool
call. Asking the user to paste a key into the chat instead would put the secret into the
model's context and into the stored conversation permanently. That difference was
measured rather than assumed: a probe against Claude Desktop 1.0.0 (protocol 2025-11-25)
and Codex desktop (codex-mcp-client 0.146.0-alpha.9.2, protocol 2025-06-18) confirmed a
value typed into a view does not reach the model, and does not appear in host logs.

Three constraints came out of that probe and are load-bearing here:

* Everything is inlined. Both hosts apply a content-security policy that blocks any
  external fetch, so a stylesheet, font or script from a CDN leaves the frame blank.
* The handshake must complete. A view that does not answer `ui/initialize` and then send
  `ui/notifications/initialized` stays collapsed with nothing shown.
* Host capabilities cannot be used as a feature test. Claude Desktop 1.0.0, the build this
  was first measured against, rendered MCP Apps without advertising the
  `io.modelcontextprotocol/ui` extension at all. Build 1.30096.5 does advertise it, read
  2026-08-16, so the declaration is no longer missing everywhere — but the rule stands,
  because gating on it would still turn the form off for anyone on the older build, and
  there is nothing to be gained by gating. A view that is asked for and cannot be shown
  costs a collapsed frame; a view that is never asked for costs the whole feature.

Not every client renders MCP Apps. `setup_credentials` therefore returns text that names
the file and the `login` command as well, so on a client that shows nothing the model
still has something correct to say.

Two later facts come from the spec itself (`src/spec.types.ts` in
modelcontextprotocol/ext-apps) rather than from the probe. The host's reply to
`ui/initialize` carries a `hostContext` with the active theme and a palette of CSS custom
properties, refreshed by a `ui/notifications/host-context-changed` notification; the view
reads both and falls back to Delta's own neutrals, so a host that sends nothing still looks
deliberate. And `prefersBorder` is the field that decides who draws the box — omitting it
left Claude Desktop drawing one and this view drawing a second inside it. There is no
`preferredSize` field; the height is whatever the view reports.

What `prefersBorder` does *not* settle is padding, and there is no field that does. Observed
in Codex: it draws the border and insets the frame by nothing, so a view that draws no
padding of its own has its text against the line, nearer to it than the host's own tool
label. Claude Desktop does inset. The view therefore pads itself, which costs a little extra
room inside on the host that already pads and is the cheaper of the two failures. That
padding lives on a wrapper rather than on `body`: on `body` it did not visibly take effect
in Codex, and `body` is the element every injected reset names, so a wrapper class is immune
to a host stylesheet that cannot be inspected from here.

Colour is split on purpose. Surfaces, text and borders prefer the host's tokens, so the
form sits inside whatever theme the client is running. Brand and semantic colours are
always Delta's own, taken from the `--brand-india-*`, `--positive-*` and `--negative-*`
families on delta.exchange, because those are what make it recognisably Delta rather than
a generic form. The one brand asset that could not come across is the Aileron typeface —
it is a web font, and fetching it would hit the same policy that blanks the frame.

Every dimension the host can decide, the host decides. The view names no type size and no
width of its own: type comes from the `--font-text-*`, `--font-heading-*` and
`--font-weight-*` tokens, and the two spacing steps are in em, so both follow whatever type
the client turns out to run. `--gap` separates one question from the next; `--gap-tight`
binds a label to the control it names. An earlier version hardcoded 14px and 13px type over
a 3/4/6/9/12/14/16px spacing ladder tuned against that base, which is what looked wrong in
Codex — Codex does not run a 14px base, and every one of those numbers was wrong there in a
way that compounded down the form.

The controls stay native, for their keyboard behaviour and for a select whose menu is drawn
outside the frame where the app's bounds cannot clip it. They are not left bare, though:
padding in em, plus colour, border and radius taken from the host's own field tokens, so a
client's field styling carries in. What makes that safe in either theme is `color-scheme`,
which the view sets from the reported theme — the fallbacks are `color-mix()` against the
`canvas` and `canvastext` system colours, so they flip with it rather than needing a literal
per scheme. `light-dark()` is used for exactly one pair, the success and danger tones, which
genuinely differ between light and dark.

Fonts are the same story from the other side: the host may send `@font-face` rules in
`hostContext.styles.css.fonts`, and the spec makes installing them the app's job, so a view
that ignores them names a family in `--font-sans` that its own frame never loaded, renders
in a substituted face, and measures its own height against that face.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from mcp.server.apps import APP_MIME_TYPE
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.session import ServerSession
from mcp.types import CallToolResult, TextContent

from delta_exchange_mcp import credentials, hints, request, store
from delta_exchange_mcp.config import (
    BASE_URLS,
    DASHBOARDS,
    DEFAULT_ENV,
    DEFAULT_MODE,
    MODES,
    load,
    mode_for_client,
    mode_key,
)

@dataclass(frozen=True)
class Activation:
    """The read and mutation surfaces that are actually live after reconciliation."""

    account_ready: bool
    mode: str
    effective_mode: str
    expected_current: bool = True


@dataclass(frozen=True)
class ExpectedState:
    """Secret-bearing write result to compare internally with activation's snapshot."""

    environment: str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    mode_setting: str = ""
    mode: str | None = None


# Given the session to notify on, brings the live surface up to date after a form save.
Activate = Callable[[ServerSession, ExpectedState], Awaitable[Activation]]

_GRANT_TTL_SECONDS = 10 * 60


@dataclass
class _Grant:
    token: str
    expires_at: float
    in_use: bool = False


_DIRECT_SESSION = object()

VIEW_URI = "ui://delta-exchange/credentials.html"

# The profile parameter is what marks an HTML resource as an app view rather than a
# document; a client renders it in a sandboxed frame instead of treating it as content.
# The SDK owns the exact string, so take it from there rather than restate it and let the
# two drift.
VIEW_MIME = APP_MIME_TYPE

# Both spellings of the same association. The nested form is what SEP-1865 specifies and
# the flat one is what earlier implementations read; sending both costs nothing and
# neither host was willing to say which it uses.
_OPENS_VIEW = {"ui": {"resourceUri": VIEW_URI}, "ui/resourceUri": VIEW_URI}

# Hides the tool from the model while leaving it callable from inside the view. Verified
# honoured by both hosts. See `register` for what happens on a client that ignores it.
_APP_ONLY = {"ui": {"visibility": ["app"]}}

ENVIRONMENTS = [
    {
        "value": "india_prod",
        "label": "Real account",
        "site": "delta.exchange",
    },
    {
        "value": "india_testnet",
        "label": "Practice account",
        "site": "demo.delta.exchange",
    },
]

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Connect your Delta Exchange account</title>
<style id="host-fonts"></style>
<style>
  /* Nothing below names a type size or a width. Type comes from the host's typography
     tokens and both spacing steps are in em, so they track it. The controls stay native and
     take their colour, border and radius from the host's own field tokens. What is Delta's
     rather than the host's: the brand fill on the primary button, the accent on the radios
     and checkbox, the success and danger tones, and the focus ring. */
  :root {
    color-scheme: light dark;
    /* Delta's own, always. These are the colours that make it Delta rather than a form. */
    --brand: #fe6c02;
    --brand-hover: #e76202;
    --on-brand: #ffffff;
    /* The host's tokens, falling back to the platform's own system colours rather than to
       literals, so a host that sends no palette still lands on the right side of light or
       dark by way of the color-scheme above. */
    --ink: var(--color-text-primary, canvastext);
    --muted: var(--color-text-secondary, color-mix(in srgb, canvastext 66%, canvas));
    --faint: var(--color-text-tertiary, color-mix(in srgb, canvastext 48%, canvas));
    /* The one pair that needs a value per scheme rather than a mix of the system colours:
       Delta's own success and danger tones, lightened for dark exactly as delta.exchange
       lightens them. A host that sends these tokens overrides both. */
    --positive: var(--color-text-success, light-dark(#00a876, #33b991));
    --negative: var(--color-text-danger, light-dark(#dc4e4e, #ff5c5c));
    --line: var(--color-border-primary, color-mix(in srgb, canvastext 22%, canvas));
    --field: var(--color-background-secondary, color-mix(in srgb, canvastext 5%, canvas));
    --sans: var(--font-sans, ui-sans-serif, system-ui, -apple-system, sans-serif);
    --mono: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace);
    --small: var(--font-text-sm-size, .875em);
    --radius: var(--border-radius-md, 6px);
    --stroke: var(--border-width-regular, 1px);
    /* Two steps, both in em so they track the host's type rather than an assumed base.
       The tight one binds a label to the control it names and a control to the note about
       it; the loose one separates one question from the next. Having only the loose step
       is what made the form read as a flat list with nothing grouped. */
    --gap: 1em;
    --gap-tight: .35em;
  }

  /* An opaque background on either of these paints over the chat behind the frame. */
  html, body { margin: 0; background: transparent; }
  body { font-family: var(--sans); font-size: var(--font-text-md-size, 1rem);
         line-height: var(--font-text-md-line-height, 1.5); color: var(--ink); }
  /* `prefersBorder` gets a border drawn, and nothing more: a host may inset the frame by
     nothing, leaving the text against the line — closer to it than the host's own tool
     label. No field reports whether a host insets, so the view pads itself.
     On a wrapper rather than on `body`, deliberately: a host that injects a reset into the
     frame will target the elements it can name, and `body` is the one every reset names. */
  .pad { padding: var(--gap); }
  p { margin: 0 0 var(--gap); }

  .head { display: flex; align-items: center; gap: .5em; margin-bottom: var(--gap-tight); }
  .mark { width: 1.35em; height: 1.35em; flex: none; }
  h1 { font-size: var(--font-heading-xs-size, 1em); margin: 0;
       font-weight: var(--font-weight-semibold, 600);
       line-height: var(--font-heading-xs-line-height, inherit); }
  .sub, .note, legend, .lab, .reveal, #state, #done p { font-size: var(--small);
      color: var(--muted); }

  /* One wrapper per question, carrying the gap to the next. Spacing on the controls
     themselves cannot express this: a note that is empty most of the time would either
     leave a hole under the select or double the gap when it does have something to say. */
  .field { margin-bottom: var(--gap); }
  .lab { display: block; margin-bottom: var(--gap-tight); }
  .note { margin: var(--gap-tight) 0 0; }
  .note:empty { display: none; }

  fieldset { border: 0; padding: 0; margin: 0 0 var(--gap); }
  legend { padding: 0; margin-bottom: var(--gap-tight); }
  /* Block, so the whole row is a click target rather than just the words. */
  .choice { display: block; cursor: pointer; }
  .choice + .choice { margin-top: var(--gap-tight); }
  .choice input { margin: 0 .45em 0 0; }
  .choice .site { color: var(--faint); }

  /* Native controls throughout: their keyboard behaviour, their theme-correct chrome, and
     a select whose menu is drawn outside the frame and so cannot be clipped by the app's
     bounds. What is set here is only what makes them belong to this form — the host's own
     field colour, border and radius, and padding in em. The earlier version set the same
     properties in px against an assumed 14px base, which is what looked wrong in Codex. */
  /* The leading is set after `font`, which would otherwise carry the body's prose value in
     with it: 1.5 is right for a wrapping paragraph and leaves a single-line field standing
     a third taller than the text it holds. */
  input, select, button { font: inherit; line-height: 1.35; }
  input[type=text], input[type=password] { font-family: var(--mono); }
  input[type=text], input[type=password], select {
      width: 100%; box-sizing: border-box; margin: 0; padding: .45em .6em;
      color: var(--ink); background: var(--field);
      border: var(--stroke) solid var(--line); border-radius: var(--radius); }
  input[type=radio], input[type=checkbox] { accent-color: var(--brand); }
  input::placeholder { font-family: var(--sans); color: var(--faint); }

  .row { display: flex; align-items: center; justify-content: space-between;
         gap: var(--gap); flex-wrap: wrap; margin-bottom: var(--gap); }
  .reveal { display: flex; align-items: center; gap: .4em; cursor: pointer; }

  button { background: var(--brand); color: var(--on-brand); border: 0; cursor: pointer;
           font-weight: var(--font-weight-medium, 500); padding: .55em 1.15em;
           border-radius: var(--radius); }
  button:hover { background: var(--brand-hover); }
  button[aria-disabled=true], button[aria-disabled=true]:hover {
      background: color-mix(in srgb, var(--ink) 14%, transparent);
      color: var(--faint); cursor: not-allowed; }
  button.link, button.link:hover { background: none; color: var(--muted); padding: 0;
      font-size: var(--small); font-weight: inherit; text-decoration: underline;
      text-underline-offset: 2px; }
  button.link[aria-disabled=true] { background: none; color: var(--faint); }

  /* A field already has a border of its own, so the ring hugs it instead of floating
     outside; everything else keeps the offset that stops it touching the glyphs. */
  :focus-visible { outline: 2px solid var(--brand); outline-offset: 2px; }
  input[type=text]:focus-visible, input[type=password]:focus-visible,
  select:focus-visible { outline-offset: 0; }

  #state:not(:empty) { margin-top: var(--gap); }
  #state.err { color: var(--negative); }

  #done { display: none; }
  body.done #entry { display: none; }
  body.done #done { display: block; }
  /* One block of facts about the same save, so the lines sit together and only the last
     one separates from the button that follows. */
  #done p { margin-bottom: var(--gap-tight); }
  #done .next { margin-bottom: var(--gap); }
  #done .who { color: var(--positive); font-weight: var(--font-weight-semibold, 600);
               font-size: inherit; }
</style>
</head>
<body>
<div class="pad">
  <div class="head">
    <!-- Delta's own mark, inlined. The one gradient in the original is flattened to its
         midpoint: it is imperceptible at 20px, and painting it needs a fragment reference
         written in the same syntax the test bans to keep external fetches out. -->
    <svg class="mark" viewBox="0 0 53 52" aria-hidden="true">
      <path fill="#FD7D02" d="M17.834 17.334 35.166 26 52.5 17.334 17.834 0v17.334Z"/>
      <path fill="#219b21" d="M17.834 34.667V52L52.5 34.667 35.166 26l-17.332 8.667Z"/>
      <path fill="#2CB72C" d="M52.5 34.667V17.333L35.167 26 52.5 34.667Z"/>
      <path fill="#FF9300" d="M17.832 17.333v17.334L.5 26l17.332-8.667Z"/>
    </svg>
    <h1>Connect your Delta Exchange account</h1>
  </div>

  <div id="done">
    <p class="who" id="done-who"></p>
    <p id="done-where"></p>
    <p class="next" id="done-next"></p>
    <button id="again" class="link" type="button">Use a different key</button>
  </div>

  <div id="entry">
    <p class="sub">Type your key here rather than in the chat. It is saved to a file on
    this computer, and the assistant never sees it.</p>

    <fieldset id="envs">
      <legend>Where was your key created?</legend>
    </fieldset>

    <div class="field">
      <label class="lab" for="mode">What should the assistant be able to do?</label>
      <select id="mode">
        <option value="read">Read only &mdash; balances, positions and orders</option>
        <option value="trade">Read and trade &mdash; also place and cancel orders</option>
      </select>
      <p class="note" id="mode-note"></p>
    </div>

    <div class="field">
      <label class="lab" for="key">API key</label>
      <input id="key" type="password" autocomplete="off" autocapitalize="none"
             spellcheck="false" placeholder="paste it here">
    </div>

    <div class="field">
      <label class="lab" for="secret">API secret</label>
      <input id="secret" type="password" autocomplete="off" autocapitalize="none"
             spellcheck="false" placeholder="shown only once, when you create the key">
    </div>

    <div class="row">
      <label class="reveal"><input id="show" type="checkbox">Show what I typed</label>
      <button id="create" class="link" type="button" aria-disabled="true">
        Get a key &mdash; Read Data is enough</button>
    </div>

    <button id="save" type="button" aria-disabled="true">Check and save</button>
  </div>
  <div id="state" role="status" aria-live="polite"></div>
</div>
<script>
(function () {
  var CONFIG = __CONFIG__;
  var PROTOCOL = "2026-01-26";
  var nextId = 1;
  var pending = {};
  var ready = false;
  var saving = false;
  var saveGrant = "";
  var configured = false;
  var currentEnvironment = "";

  var root = document.documentElement;
  var hostFonts = document.getElementById("host-fonts");
  var envsEl = document.getElementById("envs");
  var keyEl = document.getElementById("key");
  var secretEl = document.getElementById("secret");
  var showEl = document.getElementById("show");
  var saveEl = document.getElementById("save");
  var createEl = document.getElementById("create");
  var stateEl = document.getElementById("state");
  var modeEl = document.getElementById("mode");
  var modeNote = document.getElementById("mode-note");
  var againEl = document.getElementById("again");
  var doneWho = document.getElementById("done-who");
  var doneWhere = document.getElementById("done-where");
  var doneNext = document.getElementById("done-next");

  CONFIG.environments.forEach(function (env, i) {
    var row = document.createElement("label");
    row.className = "choice";
    var radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "env";
    radio.value = env.value;
    radio.checked = env.value === CONFIG.default_environment || (i === 0 && !CONFIG.default_environment);
    row.appendChild(radio);
    row.appendChild(document.createTextNode(env.label + " "));
    var site = document.createElement("span");
    site.className = "site";
    site.textContent = "\\u2014 " + env.site;
    row.appendChild(site);
    envsEl.appendChild(row);
  });

  function chosenEnv() {
    var picked = envsEl.querySelector("input[name=env]:checked");
    return picked ? picked.value : CONFIG.default_environment;
  }

  function selectEnv(value) {
    var radio = envsEl.querySelector('input[name="env"][value="' + value + '"]');
    if (radio) radio.checked = true;
  }

  function say(text, cls) {
    stateEl.textContent = text;
    stateEl.className = cls || "";
    resize();
  }

  function post(msg) { window.parent.postMessage(msg, "*"); }

  function request(method, params, timeoutMs) {
    var id = nextId++;
    post({ jsonrpc: "2.0", id: id, method: method, params: params || {} });
    return new Promise(function (resolve, reject) {
      pending[id] = { resolve: resolve, reject: reject };
      setTimeout(function () {
        if (pending[id]) { delete pending[id]; reject(new Error(method + " timed out")); }
      }, timeoutMs || 30000);
    });
  }

  // setup_credentials puts this capability only in tool-result metadata. It never enters
  // model-visible content, and the server accepts it once, from this MCP session only.
  function captureGrant(result) {
    var meta = result && result._meta;
    var ui = meta && meta.ui;
    if (ui && ui.saveGrant) saveGrant = ui.saveGrant;
    refreshSaveState();
  }

  // Each variable is applied only when it has a value, so a host sending part of a palette
  // leaves the rest on the fallbacks rather than blanking them.
  function applyHostContext(ctx) {
    if (!ctx) return;
    if (ctx.theme) root.style.colorScheme = ctx.theme;
    var styles = ctx.styles || {};
    var vars = styles.variables;
    if (vars) {
      Object.keys(vars).forEach(function (name) {
        if (vars[name]) root.style.setProperty(name, vars[name]);
      });
    }
    // The host ships the rules that load its own typeface, and the spec makes installing
    // them the app's job. Skipping this leaves --font-sans naming a family the frame never
    // loaded, so every measurement below is taken against a substituted face instead.
    var fonts = styles.css && styles.css.fonts;
    if (fonts) hostFonts.textContent = fonts;
    resize();
  }

  window.addEventListener("message", function (event) {
    if (event.source !== window.parent) return;
    var msg = event.data;
    if (!msg || msg.jsonrpc !== "2.0") return;
    if (msg.id !== undefined && msg.method === undefined) {
      var p = pending[msg.id];
      if (!p) return;
      delete pending[msg.id];
      if (msg.error) p.reject(new Error(msg.error.message || "request failed"));
      else p.resolve(msg.result);
      return;
    }
    // A notification carries a method and no id, so it matches neither of the branches
    // around it. This is how a theme switch mid-conversation arrives.
    if (msg.method !== undefined && msg.id === undefined) {
      if (msg.method === "ui/notifications/host-context-changed") applyHostContext(msg.params);
      if (msg.method === "ui/notifications/tool-result") captureGrant(msg.params);
      return;
    }
    // The host may call into the view; answer rather than ignore, so a host waiting on a
    // reply is not left holding a request that never settles.
    if (msg.method !== undefined && msg.id !== undefined) {
      post({ jsonrpc: "2.0", id: msg.id,
             error: { code: -32601, message: "not implemented: " + msg.method } });
    }
  });

  // scrollHeight never reports less than the frame it is measured in, so reporting it can
  // only grow the frame: one long error message would leave it tall for the rest of the
  // conversation. Forcing intrinsic sizing for the measurement reports the content itself.
  function resize() {
    var previous = root.style.height;
    root.style.height = "max-content";
    var height = Math.ceil(root.getBoundingClientRect().height);
    root.style.height = previous;
    post({ jsonrpc: "2.0", method: "ui/notifications/size-changed",
           params: { height: height } });
  }

  // aria-disabled rather than disabled, because disabling a control while it holds focus
  // drops focus to the body. It does not block clicks, so every handler checks it.
  function enable(el, on) {
    if (on) el.removeAttribute("aria-disabled");
    else el.setAttribute("aria-disabled", "true");
  }

  function off(el) { return el.getAttribute("aria-disabled") === "true"; }

  function refreshSaveState() {
    var hasKey = !!keyEl.value.trim();
    var hasSecret = !!secretEl.value.trim();
    var fullPair = hasKey && hasSecret;
    var modeOnly = configured && !hasKey && !hasSecret && chosenEnv() === currentEnvironment;
    saveEl.textContent = modeOnly ? "Save access mode" : "Check and save";
    enable(saveEl, ready && !!saveGrant && !saving && (fullPair || modeOnly));
  }

  showEl.addEventListener("change", function () {
    var type = showEl.checked ? "text" : "password";
    keyEl.type = type;
    secretEl.type = type;
  });

  keyEl.addEventListener("input", refreshSaveState);
  secretEl.addEventListener("input", refreshSaveState);
  envsEl.addEventListener("change", refreshSaveState);

  // Says what the choice costs before it is made. Trading is scoped to this client, so
  // the reassurance about the others is the part worth stating.
  function syncModeNote() {
    modeNote.textContent = modeEl.value === "trade"
      ? "Trading turns on after you restart this app. Other apps on this computer stay "
        + "read only."
      : "";
    resize();
  }

  modeEl.addEventListener("change", function () {
    syncModeNote();
    refreshSaveState();
  });

  againEl.addEventListener("click", function () {
    request("tools/call", { name: "setup_credentials", arguments: {} }, 15000)
      .then(function (result) {
        captureGrant(result);
        document.body.classList.remove("done");
        resize();
        keyEl.focus();
      })
      .catch(function (err) { say("Could not reopen the form: " + err.message, "err"); });
  });

  createEl.addEventListener("click", function () {
    if (off(createEl)) return;
    var url = CONFIG.dashboards[chosenEnv()];
    if (!url) { say("No key page for that environment.", "err"); return; }
    request("ui/open-link", { url: url }).catch(function (err) {
      say("This client would not open a link. Go to " + url + " yourself.", "err");
    });
  });

  saveEl.addEventListener("click", function () {
    if (off(saveEl) || saving) return;
    var key = keyEl.value.trim();
    var secret = secretEl.value.trim();
    var modeOnly = configured && !key && !secret && chosenEnv() === currentEnvironment;
    saving = true;
    refreshSaveState();
    say(modeOnly ? "Saving the access mode\\u2026" : "Checking the key against Delta\\u2026");
    // Longer than the default: this call asks Delta about the key, and the client behind
    // it backs off and retries a rate limit. Timing out sooner than the work can finish
    // would report a failure over a key that was in fact saved.
    request("tools/call", {
      name: modeOnly ? "save_mode" : "save_credentials",
      arguments: modeOnly
        ? { mode: modeEl.value, grant: saveGrant }
        : {
            environment: chosenEnv(), api_key: key, api_secret: secret,
            mode: modeEl.value, grant: saveGrant,
          },
    }, 120000).then(function (result) {
      var payload = readResult(result);
      var status = payload.status || "failed";
      // A superseded save was immediately replaced by another client, but the sensitive
      // fields still must be cleared: this process handled them and the grant is consumed.
      var handled = status === "saved" || status === "unverified"
        || status === "overridden" || status === "superseded";
      if (handled) {
        saveGrant = "";
        if (status !== "superseded") {
          configured = true;
          currentEnvironment = chosenEnv();
        }
        // Do not leave a secret sitting in a rendered field once it is stored.
        keyEl.value = "";
        secretEl.value = "";
        showEl.checked = false;
        keyEl.type = secretEl.type = "password";
      }
      saving = false;
      refreshSaveState();
      // Only a clean save swaps the form out. The other two stored cases still need the
      // fields, because what they say is "saved, and here is what to fix".
      if (status === "saved" && (payload.account || payload.mode_updated)) {
        doneWho.textContent = payload.account
          ? "Connected as " + payload.account
          : "Access mode updated";
        doneWhere.textContent = "Saved to " + (payload.path || "this computer");
        doneNext.textContent = payload.next_step || "";
        againEl.textContent = payload.account ? "Use another key" : "Change again";
        document.body.classList.add("done");
        say("");
        return;
      }
      say(payload.message || status, handled && status !== "superseded" ? "" : "err");
    }).catch(function (err) {
      saving = false;
      refreshSaveState();
      say("Could not tell whether it saved: " + err.message +
          ". Restart this client and ask whether your account is connected.", "err");
    });
  });

  function readResult(result) {
    // Hosts differ over whether a tool result arrives parsed in structuredContent or as
    // JSON text; try the parsed form first and fall back rather than pick one.
    if (result && result.structuredContent) return result.structuredContent;
    var content = (result && result.content) || [];
    for (var i = 0; i < content.length; i++) {
      if (content[i] && content[i].type === "text") {
        try { return JSON.parse(content[i].text); } catch (e) { return { message: content[i].text }; }
      }
    }
    return {};
  }

  request("ui/initialize", {
    appCapabilities: {},
    appInfo: { name: "Delta Exchange credentials", version: "1" },
    protocolVersion: PROTOCOL,
  }).then(function (result) {
    post({ jsonrpc: "2.0", method: "ui/notifications/initialized" });
    ready = true;
    enable(createEl, true);
    refreshSaveState();
    // The theme and the palette arrive here. Discarding this result is what left the view
    // styling itself off the operating system rather than off the client it renders in.
    applyHostContext(result && result.hostContext);
    // The mode already in force for this client. Without asking, the control would show
    // "Read only" to someone who had already enabled trading, and saving would quietly
    // take it away again.
    request("tools/call", { name: "get_connection_status", arguments: {} }, 15000)
      .then(function (status) {
        var now = readResult(status);
        // What it will be after a restart, not what is live: someone who chose trading a
        // moment ago must not be shown "Read only" and quietly downgraded on the next save.
        var current = now && (now.mode_after_restart || now.mode);
        if (current) { modeEl.value = current; syncModeNote(); }
        if (now && now.environment) {
          currentEnvironment = now.environment;
          selectEnv(now.environment);
        }
        configured = !!(now && now.credentials_configured);
        refreshSaveState();
      })
      .catch(function () {});
    resize();
  }).catch(function (err) {
    say("This client could not open the form: " + err.message, "err");
  });

  window.addEventListener("resize", resize);
  resize();
})();
</script>
</body>
</html>
"""

VIEW_HTML = _TEMPLATE.replace(
    "__CONFIG__",
    json.dumps(
        {
            "environments": ENVIRONMENTS,
            "dashboards": DASHBOARDS,
            "default_environment": DEFAULT_ENV,
        }
    ),
)


def build_id() -> str:
    """A short fingerprint of the view this process would serve.

    The package version cannot answer "am I looking at the current form?" — every commit on
    a branch reports the same one, and a client that reused a cached build looks identical
    to one that fetched. Two rounds of this were spent reading a package cache to work out
    which revision a screenshot came from. Hashing the served bytes answers it directly, and
    answers it about the artifact in question rather than about git metadata that a source
    install does not carry anyway.
    """
    return hashlib.sha256(VIEW_HTML.encode()).hexdigest()[:10]


# Delta spells each of these two ways depending on the endpoint.
_KEY_NOT_FOUND = {"InvalidApiKey", "invalid_api_key"}
_NO_PERMISSION = {"UnauthorizedApiAccess", "unauthorized_api_access"}
_IP_BLOCKED = {"ip_not_whitelisted_for_api_key"}


def _rejection(env: str, result: credentials.Check) -> str:
    """What the form says when Delta turns the key down.

    Deliberately replaces the message from `errors.py` rather than adding to it. That one
    is written for a log — it opens with `delta api error:`, quotes the raw code, names
    DELTA_MCP_ENV and ends in `[http 401]` — and none of that is actionable for someone
    looking at two text fields and a pair of radio buttons. Each case here names the thing
    on screen they can change instead.

    Anything without copy of its own keeps the raw message, because for a failure nobody
    anticipated it is the only information there is.
    """
    chosen = next((e for e in ENVIRONMENTS if e["value"] == env), None)
    other = next((e for e in ENVIRONMENTS if e["value"] != env), None)

    if result.code in _KEY_NOT_FOUND and chosen and other:
        return (
            f"Delta does not recognise this key on {chosen['site']}. A key only works on "
            f"the site it was created on — if you made this one on {other['site']}, pick "
            f"{other['label']} above. Otherwise check that both the key and the secret "
            "were pasted in full."
        )
    if result.code in _NO_PERMISSION:
        return (
            "This key cannot read your account. Give it the Read Data permission under "
            "Account → API Keys on Delta, then save again."
        )
    if result.code in _IP_BLOCKED:
        seen = f" It saw {result.ip}." if result.ip else ""
        return (
            "Delta blocked this request because the key only accepts whitelisted IP "
            f"addresses and this computer is not on its list.{seen} Add it to the key "
            "under Account → API Keys, then save again."
        )
    return f"Delta rejected this key. {result.detail}"


def _override_message(overridden: list[str]) -> str:
    """What to say when the client's own configuration outranks what was just saved.

    The two cases fail differently and need saying differently. A client supplying its own
    key discards this one outright. A client supplying only the environment still uses this
    key, against the site it was not created on, where Delta rejects it as unknown.
    """
    names = set(overridden)
    if {"DELTA_API_KEY", "DELTA_API_SECRET"} & names:
        consequence = "so this key will not be used at all"
    elif "DELTA_MCP_ENV" in names:
        consequence = (
            "so your key will be used against the other site, where Delta will reject it "
            "as a key it has never seen"
        )
    else:
        consequence = (
            "so the mode selected here is not the mode this client will run; change "
            "DELTA_MCP_MODE in this client's own configuration"
        )
    return (
        f"Saved to {store.path()}, but this client sets {', '.join(overridden)} in its own "
        f"configuration, and that beats the file — {consequence}. Clear those from this "
        "client's MCP entry, or from the fields it asked you to fill in when you installed "
        "it, and then restart it. Restarting on its own will not help, because the client "
        "passes its own value again every time it starts."
    )


def _client_name(ctx: Context) -> str:
    """What the connected client called itself, or "" when there is no session to ask.

    The session hangs off the request context, and the SDK raises rather than returning
    None when there is none — which is what calling the tool in-process does, as the
    tests do. An empty name is handled by the caller, since a trading mode that cannot be
    scoped to a client must not be written at all.
    """
    try:
        session = ctx.session
    except ValueError:
        return ""
    return request.client(session).name


def _opened_message() -> str:
    """What the model is told after opening the form.

    Deliberately says what *not* to do first. Left to itself a model asked for help with
    an API key will offer to take it in the chat, which is the one outcome this whole
    module exists to prevent, and the offer sounds helpful enough that people accept it.
    """
    return (
        "A form is now open in this conversation. Tell the user to type their API key "
        "and secret into it — never ask them to send a key or secret as a chat message, "
        "because anything sent that way is stored in this conversation and visible to "
        "you. You will not see what they type or whether it saved: call "
        "get_connection_status once they say they are done, which reports whether a key "
        "is configured and whether this client still has to be restarted. If no form "
        "appeared, this client cannot display one — tell them to run "
        "`uvx delta-exchange-mcp login` in a terminal, or to open "
        f"{store.path()} and fill in DELTA_API_KEY and DELTA_API_SECRET, then to restart "
        "this client."
    )


def register(mcp: MCPServer, activate: Activate | None = None) -> None:
    """Add the credential form and the two tools that drive it.

    `activate` brings up the surface a newly saved credential unlocks, in the running
    process, and reports whether anything is still outstanding. Without it a save can only
    end in "restart this client", which is a poor thing to say to someone in the middle of
    a conversation. It is optional so a test can register the form on its own rather than
    build a whole server.

    The host-enforced `_meta.ui.visibility=["app"]` is the primary boundary around both
    save tools. A short-lived one-use grant adds defence in depth for a host that exposes
    an app-only schema to its model accidentally. Standard tools/call carries no trustworthy
    app-versus-model provenance, so this cannot defend against a malicious host that leaks
    the tool result metadata as well; the grant is deliberately documented as a second gate,
    not as authenticated user presence.
    """
    # Keep the connection object itself as the key. An integer id can be reused after a
    # connection closes while its grant is still live, which would let a new client inherit
    # that old grant. Retaining the object for at most the grant TTL makes that identity
    # unambiguous and bounds the lifetime of the reference.
    grants: dict[object, _Grant] = {}

    def session_key(ctx: Context) -> object:
        try:
            return request.peer(ctx.session)
        except ValueError:
            # Direct in-process tests have no protocol session. They still exercise the
            # grant lifecycle under one sentinel rather than bypassing it.
            return _DIRECT_SESSION

    def clean_grants() -> None:
        now = time.monotonic()
        for key, pending in list(grants.items()):
            if pending.expires_at <= now:
                del grants[key]

    def issue_grant(ctx: Context) -> _Grant:
        clean_grants()
        pending = _Grant(
            token=secrets.token_urlsafe(32),
            expires_at=time.monotonic() + _GRANT_TTL_SECONDS,
        )
        grants[session_key(ctx)] = pending
        return pending

    def begin_grant(ctx: Context, offered: str) -> tuple[object, _Grant] | None:
        clean_grants()
        key = session_key(ctx)
        pending = grants.get(key)
        if (
            pending is None
            or pending.in_use
            or not offered
            or not hmac.compare_digest(pending.token, offered)
        ):
            return None
        pending.in_use = True
        return key, pending

    def finish_grant(key: object, pending: _Grant, *, used: bool) -> None:
        if grants.get(key) is not pending:
            return
        if used:
            del grants[key]
        else:
            pending.in_use = False

    def next_step(
        client: str,
        effective: str,
        live_state: Activation,
        *,
        mode_overridden: bool = False,
    ) -> str:
        reads = (
            "Your account tools are live in this session — just ask about your account. "
            "If this client does not show them yet, restart it."
            if live_state.account_ready
            else "Restart this client to use your account."
        )
        scope = f"{client!r}, the name this client reported" if client else "this client"
        if mode_overridden:
            return (
                f"{reads} This session is currently running in {live_state.mode} mode. "
                "DELTA_MCP_MODE in this client's own configuration makes future sessions "
                f"{effective} for {scope}; change that setting before restarting if that is "
                "not what you want."
            )
        if effective == "trade":
            if live_state.mode == "trade":
                return f"{reads} Trading is live for {scope}."
            return (
                f"{reads} Restart this app to turn trading on for {scope}; other apps on "
                "this computer stay read only."
            )
        return f"{reads} Trading stays off for {scope}."

    # Not read-only: opening mints a one-use grant, which is process state. Harmless and
    # repeatable though, so nothing here asks a client to hesitate.
    @mcp.tool(
        annotations=hints.mutates(
            "Connect your Delta account",
            destructive=False,
            idempotent=True,
            external=False,
        ),
        meta=_OPENS_VIEW,
    )
    async def setup_credentials(ctx: Context) -> CallToolResult:
        """Open a form for the user to enter their Delta API key, kept out of the chat.

        Call this whenever the user wants to log in, sign in, connect their Delta
        account, add or replace an API key, turn trading on or off for this client, or
        when an account tool is unavailable because none is configured. Call this first
        even when they say "login" — the `login` terminal command is the fallback for
        clients that cannot display a form, and whether this one can is reported back to
        you by this tool. Never ask for the key or secret in the conversation instead.
        """
        message = _opened_message()
        pending = issue_grant(ctx)
        return CallToolResult(
            content=[TextContent(type="text", text=message)],
            structuredContent={"status": "form_opened", "instructions": message},
            _meta={
                "ui": {
                    "saveGrant": pending.token,
                    "saveGrantExpiresIn": _GRANT_TTL_SECONDS,
                }
            },
        )

    # External because saving verifies the candidate key against Delta before writing it.
    @mcp.tool(
        annotations=hints.mutates(
            "Save your Delta credentials", destructive=True, idempotent=True
        ),
        meta=_APP_ONLY,
    )
    async def save_credentials(
        environment: str,
        api_key: str,
        api_secret: str,
        grant: str,
        ctx: Context,
        mode: str = "read",
    ) -> dict[str, str]:
        """Save a key typed into the credential form. Called by the form, not by you.

        The values come from what the user typed inside the form's own frame. Do not
        call this yourself, and do not ask the user for these values in the chat.
        """
        claimed = begin_grant(ctx, grant)
        if claimed is None:
            return {
                "status": "refused",
                "message": "This form session expired or was already used. Open it again.",
            }
        grant_key, pending = claimed

        env = (environment or "").strip().lower()
        if env not in BASE_URLS:
            finish_grant(grant_key, pending, used=False)
            return {
                "status": "invalid",
                "message": f"{environment!r} is not one of {sorted(BASE_URLS)}.",
            }

        wanted = (mode or "").strip().lower() or DEFAULT_MODE
        if wanted not in MODES:
            finish_grant(grant_key, pending, used=False)
            return {
                "status": "invalid",
                "message": f"{mode!r} is not one of {sorted(MODES)}.",
            }

        # Trading is stored against the name this client gave in the handshake, never
        # under a shared one: the settings file is read by every client on the machine,
        # and one unscoped value would arm order placement in all of them.
        client = _client_name(ctx)
        if wanted == "trade" and not mode_key(client):
            finish_grant(grant_key, pending, used=False)
            return {
                "status": "invalid",
                "message": (
                    "This client did not say who it is during the handshake, so trading "
                    "cannot be turned on for it alone. Save with read only, then set "
                    "DELTA_MCP_MODE=trade in this client's own configuration."
                ),
            }

        key = (api_key or "").strip()
        secret = (api_secret or "").strip()
        if not key or not secret:
            finish_grant(grant_key, pending, used=False)
            return {
                "status": "invalid",
                "message": (
                    "Both the key and its secret are needed — one without the other "
                    "leaves the server on market data only."
                ),
            }

        try:
            result = await credentials.check(env, key, secret)
        except BaseException:
            finish_grant(grant_key, pending, used=False)
            raise
        if result.reachable and not result.ok:
            finish_grant(grant_key, pending, used=False)
            return {"status": "rejected", "message": _rejection(env, result)}

        problem = credentials.save(env, key, secret, client, wanted)
        if problem is not None:
            finish_grant(grant_key, pending, used=False)
            return {"status": "failed", "message": problem}
        # The credential tuple is durable now. Consume before any notification can fail so
        # retrying an ambiguous response cannot write it a second time.
        finish_grant(grant_key, pending, used=True)

        # Checked after the write, not before it: the file is what every other client on
        # this machine reads, so the key still belongs there even when this one ignores it.
        overridden = credentials.overridden_by_client(client)
        if set(overridden) - {"DELTA_MCP_MODE"}:
            return {
                "status": "overridden",
                "path": str(store.path()),
                "message": _override_message(overridden),
            }

        expected = ExpectedState(
            environment=env,
            api_key=key,
            api_secret=secret,
            mode_setting=mode_key(client),
            mode=wanted if mode_key(client) else None,
        )
        live_state = (
            await activate(ctx.session, expected)
            if activate is not None
            else Activation(
                account_ready=False,
                mode="read",
                effective_mode=mode_for_client(client),
            )
        )
        effective = live_state.effective_mode
        follow_up = next_step(
            client,
            effective,
            live_state,
            mode_overridden="DELTA_MCP_MODE" in overridden,
        )
        common = {
            "path": str(store.path()),
            "next_step": follow_up,
            "effective_mode": effective,
            "client_name": client,
            "mode_setting": mode_key(client),
        }
        if not live_state.expected_current:
            return common | {
                "status": "superseded",
                "message": (
                    "Another client changed the shared Delta settings after this key was "
                    "checked and saved. This session follows those newer settings, so it "
                    "is not claiming the checked account is connected. Reopen the form if "
                    "you want to replace them."
                ),
            }
        if overridden:
            return common | {
                "status": "overridden",
                "message": f"{_override_message(overridden)} {follow_up}",
            }

        if not result.reachable:
            # Saved unverified on purpose: a flaky connection must not cost someone a key
            # they typed correctly, and the next real call will report the truth anyway.
            return common | {
                "status": "unverified",
                "message": (
                    f"Saved to {store.path()}, but Delta could not be reached to check "
                    f"it. {result.detail} {follow_up}"
                ),
            }

        # The account email is included because it is the only signal that distinguishes
        # "saved" from "saved the wrong account's key", and it is not a new disclosure:
        # any assistant with these credentials can read it from the profile endpoint.
        who = f" as {result.detail}" if result.detail else ""
        # The same facts twice: as fields, because the view renders them as its own
        # connected state rather than printing a paragraph, and as one sentence, because
        # that is what a client without a view has to show.
        return common | {
            "status": "saved",
            "account": result.detail,
            "message": f"Connected{who}. Saved to {store.path()}. {follow_up}",
        }

    @mcp.tool(
        annotations=hints.mutates(
            "Set what the assistant may do",
            destructive=True,
            idempotent=True,
            external=False,
        ),
        meta=_APP_ONLY,
    )
    async def save_mode(mode: str, grant: str, ctx: Context) -> dict[str, str]:
        """Change this client's access mode without reading or resubmitting its key."""
        claimed = begin_grant(ctx, grant)
        if claimed is None:
            return {
                "status": "refused",
                "message": "This form session expired or was already used. Open it again.",
            }
        grant_key, pending = claimed
        wanted = (mode or "").strip().lower() or DEFAULT_MODE
        if wanted not in MODES:
            finish_grant(grant_key, pending, used=False)
            return {
                "status": "invalid",
                "message": f"{mode!r} is not one of {sorted(MODES)}.",
            }
        client = _client_name(ctx)
        binding = mode_key(client)
        if not binding:
            finish_grant(grant_key, pending, used=False)
            return {
                "status": "invalid",
                "message": "This client did not report a name, so its mode cannot be scoped.",
            }
        if not load().has_credentials:
            finish_grant(grant_key, pending, used=False)
            return {
                "status": "invalid",
                "message": "Connect an account before changing its access mode.",
            }
        problem = credentials.save_mode(client, wanted)
        if problem is not None:
            finish_grant(grant_key, pending, used=False)
            return {"status": "failed", "message": problem}
        finish_grant(grant_key, pending, used=True)

        expected = ExpectedState(mode_setting=binding, mode=wanted)
        live_state = (
            await activate(ctx.session, expected)
            if activate is not None
            else Activation(
                account_ready=False,
                mode="read",
                effective_mode=mode_for_client(client),
            )
        )
        overridden = [
            name
            for name in credentials.overridden_by_client(client)
            if name == "DELTA_MCP_MODE"
        ]
        effective = live_state.effective_mode
        follow_up = next_step(
            client, effective, live_state, mode_overridden=bool(overridden)
        )
        common = {
            "mode_updated": "true",
            "path": str(store.path()),
            "next_step": follow_up,
            "effective_mode": effective,
            "client_name": client,
            "mode_setting": binding,
        }
        if not live_state.expected_current:
            return common | {
                "status": "superseded",
                "message": (
                    "Another client changed this access-mode setting before it could be "
                    "activated. This session follows the newer setting; reopen the form "
                    "if you still want to change it."
                ),
            }
        if overridden:
            return common | {
                "status": "overridden",
                "message": f"{_override_message(overridden)} {follow_up}",
            }
        return common | {
            "status": "saved",
            "message": f"Saved the access mode for {client!r}. {follow_up}",
        }

    # Named and titled rather than left to the function name: a host lists this resource
    # to the user, and `credentials_view` is not something anyone asked to open.
    @mcp.resource(
        VIEW_URI,
        name="credentials-form",
        title="Connect your Delta Exchange account",
        description="A form for entering a Delta API key without putting it in the chat.",
        mime_type=VIEW_MIME,
        # `prefersBorder` is what decides who draws the box. Omitting it left Claude
        # Desktop drawing one and this view drawing a second inside it. There is no
        # `preferredSize` in the spec — the height comes from what the view reports.
        meta={"ui": {"prefersBorder": True}},
    )
    def credentials_view() -> str:
        return VIEW_HTML
