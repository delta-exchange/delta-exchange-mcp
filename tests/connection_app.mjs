import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(0, "utf8");
const version = "2026-01-26";
const url = "http://127.0.0.1:12345/disposable-test-path";
const toolResult = { _meta: { ui: { manageUrl: url } } };

function view() {
  const messages = [];
  const listeners = new Map();
  const elements = new Map(["status", "open"].map(id => [id, {
    textContent: "",
    hidden: true,
    addEventListener: (event, callback) => listeners.set(`${id}:${event}`, callback),
  }]));
  const parent = { postMessage: message => messages.push(message) };
  const window = { parent, addEventListener: (event, callback) => listeners.set(event, callback) };
  const document = { getElementById: id => elements.get(id) };
  vm.runInNewContext(source, { window, document });
  const receive = data => listeners.get("message")({ source: parent, data: { jsonrpc: "2.0", ...data } });
  const initialize = result => receive({ id: messages[0].id, result });
  return { messages, elements, listeners, receive, initialize };
}

const flush = () => new Promise(resolve => setImmediate(resolve));

for (const failure of [false, "result", "rpc"]) {
  const app = view();
  assert.equal(app.messages[0].params.protocolVersion, version);
  app.receive({ method: "ui/notifications/tool-result", params: toolResult });
  assert.equal(app.messages.length, 1, "do not open before negotiation");
  app.initialize({ protocolVersion: version, hostCapabilities: { openLinks: {} } });
  await flush();
  assert.equal(app.messages[1].method, "ui/notifications/initialized");
  const open = app.messages[2];
  assert.equal(open.method, "ui/open-link");
  assert.equal(open.params.url, url);
  app.receive(failure === "rpc"
    ? { id: open.id, error: { code: -32603, message: "cannot open" } }
    : { id: open.id, result: { isError: failure === "result" } });
  await flush();
  assert.equal(app.elements.get("status").textContent, failure
    ? "This client could not open the connection page."
    : "Connection page opened.");
  if (failure) {
    assert.equal(app.elements.get("open").hidden, false);
    app.listeners.get("open:click")();
    assert.equal(app.messages.at(-1).method, "ui/open-link");
  }
}

const unsupported = view();
unsupported.initialize({ protocolVersion: "2026-07-28", hostCapabilities: { openLinks: {} }, toolResult });
await flush();
assert.equal(unsupported.messages.length, 1);
assert.equal(unsupported.elements.get("status").textContent, "This client could not initialize the connection view.");

const noLinks = view();
noLinks.initialize({ protocolVersion: version, hostCapabilities: {}, toolResult });
await flush();
assert.equal(noLinks.messages.length, 2);
assert.equal(noLinks.elements.get("open").hidden, true);
assert.equal(noLinks.elements.get("status").textContent, "Open the connection link in the tool response.");
