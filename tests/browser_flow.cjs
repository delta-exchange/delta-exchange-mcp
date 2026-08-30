// Execute the generated script with a small DOM and loopback-response test double.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor() {
    this.children = [];
    this.handlers = new Map();
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.value = "";
    this.textContent = "";
  }

  appendChild(child) { this.children.push(child); }
  addEventListener(event, handler) { this.handlers.set(event, handler); }

  querySelector(selector) {
    const radios = this.children.flatMap((label) => label.children);
    if (selector.includes(":checked")) return radios.find((radio) => radio.checked);
    const value = selector.match(/input\[value="([^"]+)"\]/);
    assert.ok(value, `Unexpected selector: ${selector}`);
    return radios.find((radio) => radio.value === value[1]);
  }
}

async function main() {
  const html = fs.readFileSync(0, "utf8");
  const script = html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)]
    .map((match) => [match[1], new Element()]));
  const buttons = [...html.matchAll(/<button\b[^>]*\bid="([^"]+)"/g)]
    .map((match) => elements.get(match[1]));
  const inputs = [...html.matchAll(/<input\b[^>]*\bid="([^"]+)"/g)]
    .map((match) => elements.get(match[1]));
  const document = {
    getElementById(id) {
      assert.ok(elements.has(id), `Missing element: ${id}`);
      return elements.get(id);
    },
    createElement() { return new Element(); },
    createTextNode(text) { return { textContent: text }; },
    querySelectorAll(selector) {
      if (selector === "button") return buttons;
      if (selector === "input, button") return [...inputs, ...buttons];
      assert.fail(`Unexpected selector: ${selector}`);
    },
  };
  const connection = (enabled) => ({
    environment: "india_prod",
    client_name: "Browser test",
    environments: {
      india_prod: {
        active: true, connected: true, validation_state: "verified",
        account_id: "test-account", credential_source: "operating_system",
      },
    },
    trading: { enabled },
  });
  const calls = [];
  const fetch = async (endpoint, options) => {
    assert.equal(endpoint, "/rpc");
    const payload = JSON.parse(options.body);
    calls.push(payload.action);
    if (calls.length > 2) throw new Error("The loopback listener is closed");
    const complete = payload.action === "consent";
    const content = complete
      ? { status: "enabled", message: "Trading enabled for this client.", connection: connection(true) }
      : connection(false);
    return {
      ok: true,
      headers: { get() { return "next-csrf"; } },
      async json() { return { complete, result: { structuredContent: content } }; },
    };
  };

  vm.runInNewContext(script, { document, fetch });
  await new Promise(setImmediate);
  assert.equal(elements.get("connection-state").textContent, "Connected");
  assert.equal(elements.get("enable-trading").disabled, false);
  assert.equal(elements.get("acknowledge").checked, false);

  elements.get("acknowledge").checked = true;
  elements.get("enable-trading").handlers.get("click")();
  await new Promise(setImmediate);

  assert.deepEqual(calls, ["status", "consent"], "Completion must not fetch the closed listener");
  assert.equal(elements.get("trading-state").textContent, "Trading is enabled for Browser test.");
  assert.match(elements.get("notice").textContent, /Return to your MCP client/);
  assert.equal(elements.get("notice").className, "good");
  assert.ok([...inputs, ...buttons].every((element) => element.disabled));
  assert.equal(elements.get("prod-ack").hidden, true);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
