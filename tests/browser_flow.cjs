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
    // A real radio group clears the prior choice. This small DOM keeps plain objects, so
    // prefer the last checked radio to model the programmatic selection made by render().
    if (selector.includes(":checked")) return radios.findLast((radio) => radio.checked);
    const value = selector.match(/input\[value="([^"]+)"\]/);
    assert.ok(value, `Unexpected selector: ${selector}`);
    return radios.find((radio) => radio.value === value[1]);
  }
}

async function run(html, reconnect, devnet = false) {
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
  let isConnected = !reconnect;
  const activeEnvironment = devnet ? "india_devnet" : "india_prod";
  const connection = (enabled) => ({
    environment: activeEnvironment,
    client_name: "Browser test",
    environments: {
      india_prod: {
        active: !devnet, connected: !devnet && isConnected, validation_state: "verified",
        account_id: "test-account", credential_source: "operating_system",
        reconnect_required: !devnet && !isConnected, browser_manageable: true,
      },
      india_devnet: {
        active: devnet, connected: false, validation_state: "not_connected",
        account_id: "", credential_source: "not_connected",
        reconnect_required: false, browser_manageable: false,
      },
    },
    trading: { enabled },
  });
  const calls = [];
  const fetch = async (endpoint, options) => {
    assert.equal(endpoint, "/rpc");
    const payload = JSON.parse(options.body);
    calls.push(payload.action);
    if (calls.length > (reconnect ? 4 : 2)) throw new Error("The loopback listener is closed");
    const complete = payload.action === "consent";
    if (payload.action === "credentials") {
      assert.equal(payload.arguments.api_key, "synthetic-key");
      assert.equal(payload.arguments.api_secret, "synthetic-secret");
      isConnected = true;
    }
    const content = complete
      ? { status: "enabled", message: "Trading enabled for this client.", connection: connection(true) }
      : payload.action === "credentials"
      ? { status: "saved", message: "Connected." }
      : connection(false);
    return {
      ok: true,
      headers: { get() { return "next-csrf"; } },
      async json() { return { complete, result: { structuredContent: content } }; },
    };
  };

  vm.runInNewContext(script, { document, fetch });
  await new Promise(setImmediate);
  if (devnet) {
    assert.equal(elements.get("connection-state").textContent, "Not connected");
    assert.equal(elements.get("storage-state").textContent, "not_connected");
    assert.equal(elements.get("key").disabled, true);
    assert.equal(elements.get("secret").disabled, true);
    assert.equal(elements.get("show").disabled, true);
    assert.equal(elements.get("connect").disabled, true);
    assert.equal(elements.get("disconnect").disabled, true);
    assert.equal(elements.get("dashboard").disabled, true);
    const devnetLabel = elements.get("envs").children.find((label) =>
      label.children.some((child) => child.value === "india_devnet"));
    assert.equal(devnetLabel.hidden, false);
    return;
  }
  assert.equal(elements.get("reconnect-note").hidden, !reconnect);
  if (reconnect) {
    assert.equal(elements.get("connection-state").textContent, "Not connected");
    assert.equal(elements.get("enable-trading").disabled, true);
    elements.get("key").value = "synthetic-key";
    elements.get("secret").value = "synthetic-secret";
    elements.get("connect").handlers.get("click")();
    await new Promise(setImmediate);
    assert.equal(elements.get("reconnect-note").hidden, true);
    assert.equal(elements.get("key").value, "");
    assert.equal(elements.get("secret").value, "");
  }
  assert.equal(elements.get("connection-state").textContent, "Connected");
  assert.equal(elements.get("enable-trading").disabled, false);
  assert.equal(elements.get("acknowledge").checked, false);

  elements.get("acknowledge").checked = true;
  elements.get("enable-trading").handlers.get("click")();
  await new Promise(setImmediate);

  assert.deepEqual(calls, reconnect ? ["status", "credentials", "status", "consent"] : ["status", "consent"],
    "Completion must not fetch the closed listener");
  assert.equal(elements.get("trading-state").textContent, "Trading is enabled for Browser test.");
  assert.match(elements.get("notice").textContent, /Return to your MCP client/);
  assert.equal(elements.get("notice").className, "good");
  assert.ok([...inputs, ...buttons].every((element) => element.disabled));
  assert.equal(elements.get("prod-ack").hidden, true);
}

async function main() {
  const html = fs.readFileSync(0, "utf8");
  await run(html, false);
  await run(html, true);
  await run(html, false, true);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
