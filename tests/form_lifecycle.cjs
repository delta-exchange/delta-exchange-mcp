const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync(0, 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];

function openView() {
  const elements = new Map();
  function element() {
    const attributes = new Map();
    const classes = new Set();
    return {
      value: '', style: { setProperty() {} }, children: [], textContent: '',
      listeners: {},
      classList: { add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x) },
      appendChild(child) { this.children.push(child); },
      addEventListener(name, handler) { this.listeners[name] = handler; },
      setAttribute(name, value) { attributes.set(name, value); },
      removeAttribute(name) { attributes.delete(name); },
      getAttribute(name) { return attributes.get(name); },
      getBoundingClientRect() { return { height: 400 }; },
      querySelector(selector) {
        const radios = this.children.flatMap(child => child.children).filter(child => child.type === 'radio');
        const value = selector.match(/value="([^"]+)"/);
        return radios.find(radio => value ? radio.value === value[1] : radio.checked);
      },
      focus() {},
    };
  }
  const document = {
    documentElement: element(), body: element(),
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    },
    createElement: element, createTextNode: text => ({ textContent: text }),
  };
  const messages = [];
  const parent = { postMessage: message => messages.push(message) };
  let receive;
  const window = { parent, addEventListener(name, handler) { if (name === 'message') receive = handler; } };
  vm.runInNewContext(script, { document, window, setTimeout() {} });
  const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
  return {
    document, elements, messages, flush,
    async reply(method, result, error) {
      const message = messages.findLast(message => message.id && (message.method === method || message.params.name === method));
      assert.ok(message, `No request for ${method}`);
      receive({ source: parent, data: { jsonrpc: '2.0', id: message.id, result, error } });
      await flush();
    },
  };
}

async function initialize(view, configured, clientName = "Cursor") {
  await view.reply('ui/initialize', {});
  await view.reply('get_connection_status', { structuredContent: {
    environment: 'india_testnet', credentials_configured: configured, key_source: 'shared_file', client_name: clientName,
  } });
}

(async () => {
  const first = openView();
  await initialize(first, false);
  assert.equal(first.document.body.classList.contains('done'), false);
  assert.equal(first.messages.some(message => message.params?.name === 'get_trading_preferences'), false);
  const grant = { _meta: { ui: { saveGrant: 'test-grant' } } };
  first.elements.get('again').listeners.click();
  await first.reply('setup_credentials', grant);
  first.elements.get('key').value = 'dummy-key';
  first.elements.get('secret').value = 'dummy-secret';
  first.elements.get('key').listeners.input();
  first.elements.get('save').listeners.click();
  await first.reply('save_credentials', { structuredContent: {
    status: 'saved', account: '56766408', path: '/tmp/config.env', next_step: 'Ready',
  } });
  assert.equal(first.elements.get('done-who').textContent, 'Connected to account 56766408');
  assert.equal(first.elements.get('key').value, '');
  assert.equal(first.elements.get('secret').value, '');

  for (const clientName of ['Claude', 'claude-ai', 'Claude Desktop', 'claude-desktop', 'ClaudeDesktop', 'Cursor', 'claude-code']) {
    const restored = openView();
    await initialize(restored, true, clientName);
    await restored.reply('get_trading_preferences', { structuredContent: { result: { user_id: 123 } } });
    const next = restored.elements.get('done-next').textContent;
    if (clientName === 'Cursor' || clientName === 'claude-code') {
      assert.match(next, /live in this session/);
    } else {
      assert.match(next, /Cmd\+Q/);
      assert.doesNotMatch(next, /live in this session/);
    }
  }

  const reopened = openView();
  await initialize(reopened, true);
  assert.equal(reopened.document.body.classList.contains('done'), true);
  await reopened.reply('get_trading_preferences', { content: [ { type: 'text', text: JSON.stringify({ result: { user_id: 56766408 } }) } ] });
  assert.equal(reopened.elements.get('done-who').textContent, 'Connected to account 56766408');
  assert.equal(reopened.elements.get('key').value, '');
  reopened.elements.get('again').listeners.click();
  await reopened.reply('setup_credentials', grant);
  assert.equal(reopened.document.body.classList.contains('done'), false);

  const delayed = openView();
  await initialize(delayed, true);
  delayed.elements.get('again').listeners.click();
  await delayed.reply('setup_credentials', grant);
  assert.equal(delayed.document.body.classList.contains('done'), false);
  delayed.elements.get('key').value = 'replacement-key';
  delayed.elements.get('secret').value = 'replacement-secret';
  delayed.elements.get('key').listeners.input();
  delayed.elements.get('save').listeners.click();
  await delayed.reply('save_credentials', { structuredContent: {
    status: 'saved', account: '123', next_step: 'Replacement ready',
  } });
  await delayed.reply('get_trading_preferences', { structuredContent: { result: { user_id: 99 } } });
  assert.equal(delayed.elements.get('done-who').textContent, 'Connected to account 123');
  assert.equal(delayed.elements.get('done-next').textContent, 'Replacement ready');

  for (const field of ['key', 'secret', 'envs']) {
    const editing = openView();
    await editing.reply('ui/initialize', {});
    editing.elements.get(field).value = 'editing';
    editing.elements.get(field).listeners[field === 'envs' ? 'change' : 'input']();
    await editing.reply('get_connection_status', { structuredContent: {
      credentials_configured: true, environment: 'india_testnet', key_source: 'shared_file',
    } });
    assert.equal(editing.document.body.classList.contains('done'), false, field);
    assert.equal(editing.elements.get(field).value, 'editing');
  }

  const rejected = openView();
  await initialize(rejected, true);
  rejected.elements.get('again').listeners.click();
  await rejected.reply('setup_credentials', undefined, { message: 'denied' });
  await rejected.reply('get_trading_preferences', { structuredContent: { result: { user_id: 99 } } });
  assert.equal(rejected.document.body.classList.contains('done'), true);
  assert.equal(rejected.elements.get('done-who').textContent, 'Connected to account 99');
  assert.doesNotMatch(rejected.elements.get('done-next').textContent, /Checking/);
  assert.match(rejected.elements.get('state').textContent, /Could not reopen/);

  for (const result of [ { isError: true }, { structuredContent: { result: { user_id: 'invalid' } } } ]) {
    const failed = openView();
    await initialize(failed, true);
    await failed.reply('get_trading_preferences', result);
    assert.equal(failed.elements.get('done-who').textContent, 'Credentials configured');
    assert.match(failed.elements.get('done-next').textContent, /Could not verify/);
  }
  console.log('Form lifecycle passed: save, reopen, no key, replacement, delayed reply, verification failure');
})().catch(error => { console.error(error); process.exitCode = 1; });
