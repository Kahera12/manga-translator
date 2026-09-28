#!/usr/bin/env node
/**
 * Runtime smoke test for background.js — dev harness, not part of the extension.
 *
 * Runs the real service worker source in a vm context with a fake chrome API and
 * a fake WebSocket, then drives the flows the app and the pages actually produce:
 * register stamping, key -> tab routing, reconnect replay, tab teardown, the
 * keyboard command, popup messages and the keepalive alarm.
 *
 * Usage:  node extension/tools/smoke-background.mjs
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(root, 'background.js'), 'utf8');

const failures = [];
const passes = [];

function ok(name, condition, extra) {
  if (condition) passes.push(name);
  else failures.push(name + (extra ? ` — ${extra}` : ''));
}

function eq(name, actual, expected) {
  let shown;
  try {
    shown = JSON.stringify(actual);
  } catch (err) {
    shown = String(actual);
  }
  ok(name, actual === expected, `expected ${JSON.stringify(expected)}, got ${shown}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// --- fake WebSocket ---------------------------------------------------------

const sockets = [];

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.sent = [];
    this.listeners = new Map();
    sockets.push(this);
  }

  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }

  fire(type) {
    for (const fn of this.listeners.get(type) || []) fn({ target: this });
  }

  send(raw) {
    this.sent.push(JSON.parse(raw));
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.fire('close');
  }

  // --- test drivers ---
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.fire('open');
  }

  deliver(message) {
    for (const fn of this.listeners.get('message') || []) fn({ data: JSON.stringify(message) });
  }

  lastSent() {
    return this.sent[this.sent.length - 1];
  }
}

// --- fake chrome ------------------------------------------------------------

function emitter() {
  const listeners = [];
  return {
    addListener: (fn) => listeners.push(fn),
    fire: (...args) => listeners.forEach((fn) => fn(...args)),
    count: () => listeners.length,
  };
}

const onConnect = emitter();
const onMessage = emitter();
const onCommand = emitter();
const onRemoved = emitter();
const onUpdated = emitter();
const onAlarm = emitter();
const storageChanged = emitter();

const storageData = {};
const alarms = [];
const tabsSent = [];
const popupMessages = [];
let activeTabId = 5;

const chromeStub = {
  runtime: {
    lastError: null,
    id: 'smoke',
    onConnect,
    onMessage,
    onStartup: emitter(),
    onInstalled: emitter(),
    sendMessage: (message, cb) => {
      popupMessages.push(message);
      if (typeof cb === 'function') cb(null);
    },
  },
  alarms: {
    create: (name, options) => alarms.push({ name, options }),
    onAlarm,
  },
  storage: {
    local: {
      get: (defaults, cb) => cb({ ...defaults, ...storageData }),
      set: (patch, cb) => {
        const changes = {};
        for (const [key, value] of Object.entries(patch)) {
          changes[key] = { oldValue: storageData[key], newValue: value };
          storageData[key] = value;
        }
        if (cb) cb();
        storageChanged.fire(changes, 'local');
      },
    },
    onChanged: storageChanged,
  },
  commands: { onCommand },
  tabs: {
    query: (info, cb) => cb([{ id: activeTabId, url: 'https://reader.test/chapter/1' }]),
    sendMessage: (tabId, message, cb) => {
      tabsSent.push({ tabId, message });
      if (typeof cb === 'function') cb(null);
    },
    onRemoved,
    onUpdated,
  },
};

function makePort(tabId, url = `https://reader.test/${tabId}`) {
  const messageEmitter = emitter();
  const disconnectEmitter = emitter();
  const port = {
    name: 'mt',
    sender: { tab: { id: tabId, url, title: `Chapter ${tabId}` } },
    onMessage: messageEmitter,
    onDisconnect: disconnectEmitter,
    postMessage: () => {},
    toServer: (message) => messageEmitter.fire({ to: 'server', msg: message }),
    disconnect: () => disconnectEmitter.fire(),
  };
  return port;
}

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  WebSocket: FakeWebSocket,
  JSON,
  Number,
  String,
  Object,
  Array,
  Map,
  Set,
  Promise,
  Date,
  Math,
  Error,
  chrome: chromeStub,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'background.js' });

const sentTo = (tabId) => tabsSent.filter((entry) => entry.tabId === tabId).map((entry) => entry.message);

async function main() {
  await sleep(30); // let bootstrap() finish

  // 1. keepalive alarm and socket -----------------------------------------
  eq('the keepalive alarm is created', alarms[0] && alarms[0].name, 'mt-keepalive');
  eq('the keepalive period matches the protocol', alarms[0] && alarms[0].options.periodInMinutes, 0.4);
  eq('exactly one socket is opened', sockets.length, 1);
  eq('the socket targets the configured port', sockets[0].url, 'ws://127.0.0.1:8770/ws');

  sockets[0].open();
  eq('nothing is sent before a page registers', sockets[0].sent.length, 0);

  // 2. a page connects and registers --------------------------------------
  const portA = makePort(5);
  onConnect.fire(portA);
  ok('a freshly connected page is asked to register',
    sentTo(5).some((m) => m.type === 'mt-need-register'));

  portA.toServer({
    type: 'register',
    page: 'https://reader.test/chapter/1',
    panels: [
      { key: 'aaaa', url: 'https://cdn/1.jpg', width: 1000, height: 1000, order: 0, visible: true, doc_y: 0 },
      { key: 'bbbb', url: 'https://cdn/2.jpg', width: 1000, height: 1000, order: 1, visible: false, doc_y: 900 },
    ],
  });
  const register = sockets[0].lastSent();
  eq('register reaches the app', register.type, 'register');
  eq('the worker stamps the tab id', register.tab, 5);
  eq('the panel list is forwarded verbatim', register.panels.length, 2);
  eq('the page url is forwarded', register.page, 'https://reader.test/chapter/1');

  // 3. hello carries tab and frame ----------------------------------------
  portA.toServer({ type: 'hello', url: 'https://reader.test/chapter/1', title: 'Chapter 1' });
  const hello = sockets[0].lastSent();
  eq('hello is forwarded', hello.type, 'hello');
  eq('hello carries the protocol version', hello.version, 1);
  eq('hello carries the tab id', hello.tab, 5);
  eq('hello declares the top frame', hello.frame, 'top');
  ok('the page learns the connection state', sentTo(5).some((m) => m.type === 'mt-conn' && m.connected === true));

  // 4. panel routing by key ------------------------------------------------
  sockets[0].deliver({ type: 'panel', key: 'bbbb', image_w: 10, image_h: 10, blocks: [] });
  const routed = sentTo(5).filter((m) => m.type === 'panel');
  eq('a panel goes to the tab that owns its key', routed.length, 1);
  eq('the panel payload is untouched', routed[0].key, 'bbbb');

  // 5. an unknown key is broadcast ----------------------------------------
  const portB = makePort(6);
  onConnect.fire(portB);
  tabsSent.length = 0;
  sockets[0].deliver({ type: 'panel', key: 'unknown-key', image_w: 1, image_h: 1, blocks: [] });
  eq('an unknown key is broadcast to every page', tabsSent.length, 2);
  ok('both tabs received the broadcast', sentTo(5).length === 1 && sentTo(6).length === 1);

  // 6. progress and welcome are broadcast and remembered -------------------
  sockets[0].deliver({ type: 'welcome', version: 1, provider: 'openai', model: 'gpt-6-astra', target_language: 'Russian', batch_size: 8 });
  sockets[0].deliver({ type: 'progress', done: 12, total: 150, queued: 3, failed: 1, current: '12:3' });
  ok('progress reaches the pages so the badge can update',
    sentTo(5).some((m) => m.type === 'progress' && m.done === 12));

  // 7. reconnect replays hello and the stored register ---------------------
  const firstSocket = sockets[0];
  firstSocket.close();
  await sleep(700); // backoff 0.5 s
  eq('a dropped socket is replaced', sockets.length, 2);
  sockets[1].open();
  const replay = sockets[1].sent;
  ok('both live tabs are announced again on reconnect',
    replay.filter((m) => m.type === 'hello').length === 2, JSON.stringify(replay.map((m) => m.type)));
  ok('the stored register of each tab is replayed',
    replay.filter((m) => m.type === 'register').length === 1 &&
    replay.find((m) => m.type === 'register').tab === 5);

  // 8. a closing tab forgets its keys --------------------------------------
  portB.disconnect();
  onRemoved.fire(6);
  tabsSent.length = 0;
  sockets[1].deliver({ type: 'panel', key: 'unknown-key', image_w: 1, image_h: 1, blocks: [] });
  ok('keys of a closed tab are forgotten', sentTo(5).length === 1 && sentTo(6).length === 0);

  // 9. navigating away drops the tab's panels ------------------------------
  onUpdated.fire(5, { status: 'loading' }, { id: 5, url: 'https://reader.test/chapter/2' });
  sockets[1].sent.length = 0;
  firstSocket.open(); // no-op: kept to prove stale sockets are ignored
  ok('stale socket handlers are ignored', sockets[1].sent.length === 0);

  // 10. the keyboard command toggles the translation ----------------------
  storageData.enabled = true;
  onCommand.fire('toggle-translation');
  await sleep(20);
  eq('the command flips the stored flag', storageData.enabled, false);
  ok('the active tab is told about the new state',
    tabsSent.some((entry) => entry.tabId === 5 && entry.message.type === 'mt-settings' &&
      entry.message.settings.enabled === false));

  // 11. popup messages -----------------------------------------------------
  let reply = null;
  onMessage.fire({ type: 'mt-get-state' }, {}, (value) => { reply = value; });
  ok('the popup receives a state snapshot', !!reply);
  eq('the snapshot reports the socket state', reply.connected, true);
  eq('the snapshot reports the port', reply.port, 8770);
  eq('the snapshot reports the enabled flag', reply.enabled, false);
  eq('the snapshot reports the provider from welcome', reply.provider, 'openai');
  eq('the snapshot reports the progress', reply.progress.done, 12);
  eq('the snapshot reports the tab count', reply.tabs, 0);

  onMessage.fire({ type: 'mt-set-mode', value: 'inplace' }, {}, () => {});
  await sleep(20);
  eq('the mode is stored', storageData.mode, 'inplace');

  // 12. changing the port reconnects --------------------------------------
  onMessage.fire({ type: 'mt-set-port', value: '9001' }, {}, (value) => { reply = value; });
  await sleep(30);
  eq('a valid port is accepted', storageData.port, 9001);
  const last = sockets[sockets.length - 1];
  eq('the socket is reopened on the new port', last.url, 'ws://127.0.0.1:9001/ws');

  onMessage.fire({ type: 'mt-set-port', value: '70000' }, {}, (value) => { reply = value; });
  await sleep(10);
  ok('an out of range port is rejected', reply && reply.ok === false);

  // 13. the keepalive alarm pings an open socket ---------------------------
  last.open();
  last.sent.length = 0;
  onAlarm.fire({ name: 'mt-keepalive' });
  eq('the alarm sends a status ping', last.sent.length, 1);
  eq('the ping is a status message', last.sent[0].type, 'status');

  // 14. the alarm reconnects a dead socket ---------------------------------
  last.close();
  onAlarm.fire({ name: 'mt-keepalive' });
  eq('the alarm reconnects a closed socket', sockets.length, 4);

  for (const name of passes) console.log('PASS  ' + name);
  for (const name of failures) console.log('FAIL  ' + name);
  console.log('');
  console.log(`${passes.length}/${passes.length + failures.length} background checks passed`);
  process.exit(failures.length ? 1 : 0);
}

main().catch((err) => {
  console.error('harness error:', err);
  process.exit(1);
});
