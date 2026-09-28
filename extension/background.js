/**
 * MangaTranslator Bridge — background service worker.
 *
 * Owns the single WebSocket to the local Python app, keeps per-tab panel
 * registries, routes messages between the app and the content scripts and
 * survives service-worker restarts (every entry point is (re)bootstrapped
 * lazily, so a suspended worker reconnects on the next event).
 *
 * Every public entry point is wrapped: a thrown error must never kill the
 * worker, it is logged with a "[mt]" prefix instead.
 */
'use strict';

const TAG = '[mt]';
const HOST = '127.0.0.1';
const WS_PATH = '/ws';
const PROTOCOL_VERSION = 1;
const KEEPALIVE_ALARM = 'mt-keepalive';
// Keepalive period requested by the protocol (:0.4 min = 24 s). Chrome clamps
// alarm periods to >= 30 s, which is still enough to keep the socket warm.
const KEEPALIVE_MINUTES = 0.4;
// Reconnect backoff: 0.5 s doubling up to a 5 s cap, forever.
const BACKOFF_START_MS = 500;
const BACKOFF_MAX_MS = 5000;

const DEFAULTS = { port: 8770, mode: 'cards', enabled: true };

// ---------------------------------------------------------------- state ----

let settings = { ...DEFAULTS };
let socket = null;
let socketGen = 0;              // invalidates handlers of a replaced socket
let socketOpen = false;
let backoffMs = BACKOFF_START_MS;
let reconnectTimer = null;
let booted = false;

const tabs = new Map();        // tabId -> {url, title, panels: Map<key, panel>, register}
const keyOwner = new Map();    // panel key -> tabId (built from register messages)
const contentPorts = new Map();// tabId -> chrome.runtime.Port

let appInfo = null;            // last "welcome" payload
let appStatus = null;          // last free-form "status" snapshot
let lastError = '';
let progress = { done: 0, total: 0, queued: 0, failed: 0, current: '' };

function log(...args) {
  console.log(TAG, ...args);
}

function warn(...args) {
  console.warn(TAG, ...args);
}

// ------------------------------------------------------------- helpers -----

/** Callback-style storage read wrapped in a promise (works on every Chrome). */
function storageGet(defaults) {
  return new Promise((resolve) => {
    try {
      chrome.storage.local.get(defaults, (value) => {
        void chrome.runtime.lastError;
        resolve(value && typeof value === 'object' ? value : { ...defaults });
      });
    } catch (err) {
      warn('storage.get failed', err);
      resolve({ ...defaults });
    }
  });
}

/** Callback-style storage write wrapped in a promise. */
function storageSet(patch) {
  return new Promise((resolve) => {
    try {
      chrome.storage.local.set(patch, () => {
        void chrome.runtime.lastError;
        resolve();
      });
    } catch (err) {
      warn('storage.set failed', err);
      resolve();
    }
  });
}

/** Normalise anything that came out of storage into a usable settings object. */
function normalizeSettings(raw) {
  const port = Number.parseInt(raw && raw.port, 10);
  return {
    port: Number.isInteger(port) && port > 0 && port <= 65535 ? port : DEFAULTS.port,
    mode: raw && raw.mode === 'inplace' ? 'inplace' : 'cards',
    enabled: !(raw && raw.enabled === false),
  };
}

function publicSettings() {
  return { port: settings.port, mode: settings.mode, enabled: settings.enabled };
}

function publicState() {
  return {
    connected: socketOpen,
    host: HOST,
    endpoint: `ws://${HOST}:${settings.port}${WS_PATH}`,
    port: settings.port,
    mode: settings.mode,
    enabled: settings.enabled,
    provider: (appInfo && appInfo.provider) || '',
    model: (appInfo && appInfo.model) || '',
    targetLanguage: (appInfo && appInfo.target_language) || '',
    batchSize: (appInfo && appInfo.batch_size) || 0,
    progress: { ...progress },
    error: lastError,
    status: appStatus,
    tabs: tabs.size,
  };
}

/**
 * Message to a single tab.
 *
 * The content script listens on its long-lived port, so the port is tried first —
 * `chrome.tabs.sendMessage` alone reaches nobody (it addresses a
 * `chrome.runtime.onMessage` listener, which the content script does not have) and
 * the translations were silently dropped. `tabs.sendMessage` stays as a fallback
 * for the moment between a worker restart and the port being re-established.
 */
function deliverToTab(tabId, message) {
  const port = contentPorts.get(tabId);
  if (port) {
    try {
      port.postMessage(message);
      return;
    } catch (err) {
      warn('port.postMessage failed', tabId, err);
      contentPorts.delete(tabId);
    }
  }
  try {
    chrome.tabs.sendMessage(tabId, message, () => void chrome.runtime.lastError);
  } catch (err) {
    warn('tabs.sendMessage failed', tabId, err);
  }
}

/** Message every tab that still has a live content-script port. */
function broadcastToContent(message) {
  for (const tabId of contentPorts.keys()) deliverToTab(tabId, message);
}

/** Message every open extension page (the popup). */
function broadcastState() {
  try {
    chrome.runtime.sendMessage({ type: 'mt-state', state: publicState() }, () => void chrome.runtime.lastError);
  } catch (err) {
    /* no popup open — nothing to do */
  }
}

// ------------------------------------------------------------ websocket ----

function sendToServer(message) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return false;
  try {
    socket.send(JSON.stringify(message));
    return true;
  } catch (err) {
    warn('socket.send failed', err);
    return false;
  }
}

function ensureConnected() {
  try {
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
    const url = `ws://${HOST}:${settings.port}${WS_PATH}`;
    const gen = ++socketGen;
    const ws = new WebSocket(url);
    socket = ws;
    ws.addEventListener('open', () => {
      if (gen !== socketGen) return;
      onSocketOpen();
    });
    ws.addEventListener('message', (event) => {
      if (gen !== socketGen) return;
      onSocketMessage(event.data);
    });
    ws.addEventListener('error', () => {
      // The close event always follows an error; nothing to do but wait for it.
    });
    ws.addEventListener('close', () => {
      if (gen !== socketGen) return;
      onSocketClose();
    });
    log('connecting to', url);
  } catch (err) {
    warn('connect failed', err);
    scheduleReconnect();
  }
}

function closeSocket() {
  const ws = socket;
  socketGen++;                 // invalidate pending handlers of the old socket
  socket = null;
  const wasOpen = socketOpen;
  socketOpen = false;
  try {
    if (ws && ws.readyState !== WebSocket.CLOSED) ws.close();
  } catch (err) {
    /* already gone */
  }
  if (wasOpen) broadcastToContent({ type: 'mt-conn', connected: false });
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  const delay = backoffMs;
  backoffMs = Math.min(backoffMs * 2, BACKOFF_MAX_MS);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    ensureConnected();
  }, delay);
}

function onSocketOpen() {
  socketOpen = true;
  backoffMs = BACKOFF_START_MS;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  log('connected');
  // (Re)announce ourselves and replay the last register of every live tab so a
  // restarted app can pick the session up where it left off.
  for (const [tabId, tab] of tabs) {
    sendToServer({ type: 'hello', version: PROTOCOL_VERSION, url: tab.url || '', title: tab.title || '', tab: tabId, frame: 'top' });
    if (tab.register) sendToServer(tab.register);
  }
  broadcastToContent({ type: 'mt-conn', connected: true });
  broadcastState();
}

function onSocketClose() {
  socketOpen = false;
  socket = null;
  log('disconnected, retrying in', backoffMs, 'ms');
  broadcastToContent({ type: 'mt-conn', connected: false });
  broadcastState();
  scheduleReconnect();
}

function onSocketMessage(raw) {
  let message = null;
  try {
    message = JSON.parse(typeof raw === 'string' ? raw : String(raw));
  } catch (err) {
    warn('bad json from app', err);
    return;
  }
  if (!message || typeof message !== 'object' || typeof message.type !== 'string') return;
  try {
    routeFromServer(message);
  } catch (err) {
    warn('route failed', err);
  }
}

/** App -> extension routing. Panel payloads go to the tab that owns the key. */
function routeFromServer(message) {
  switch (message.type) {
    case 'panel': {
      const owner = keyOwner.get(String(message.key));
      if (owner != null && contentPorts.has(owner)) {
        deliverToTab(owner, message);
      } else {
        // Unknown key (tab gone, or register lost): broadcast and let the
        // content scripts ignore keys they do not know about.
        broadcastToContent(message);
      }
      break;
    }
    case 'welcome':
      appInfo = message;
      lastError = '';
      broadcastToContent(message);
      broadcastState();
      break;
    case 'progress':
      progress = {
        done: Number(message.done) || 0,
        total: Number(message.total) || 0,
        queued: Number(message.queued) || 0,
        failed: Number(message.failed) || 0,
        current: typeof message.current === 'string' ? message.current : '',
      };
      broadcastToContent(message);
      broadcastState();
      break;
    case 'error':
      lastError = typeof message.message === 'string' ? message.message : 'unknown error';
      warn('app error', lastError, message.kind || '');
      broadcastToContent(message);
      broadcastState();
      break;
    case 'status':
      appStatus = message;
      broadcastToContent(message);
      broadcastState();
      break;
    default:
      broadcastToContent(message);
      break;
  }
}

// -------------------------------------------------------------- tabs -------

function ensureTab(tabId) {
  let tab = tabs.get(tabId);
  if (!tab) {
    tab = { url: '', title: '', panels: new Map(), register: null };
    tabs.set(tabId, tab);
  }
  return tab;
}

/** Drop a tab's panels and forget every key that pointed at it. */
function forgetTab(tabId) {
  const tab = tabs.get(tabId);
  if (tab) {
    for (const key of tab.panels.keys()) keyOwner.delete(key);
  }
  tabs.delete(tabId);
}

function onRegister(tabId, message) {
  const tab = ensureTab(tabId);
  const panels = Array.isArray(message.panels) ? message.panels : [];
  if (typeof message.page === 'string' && message.page) tab.url = message.page;
  // Rebuild the whole key map of this tab: the content script always sends the
  // full known list, so keys that disappeared must lose their owner.
  for (const key of tab.panels.keys()) keyOwner.delete(key);
  tab.panels = new Map();
  for (const panel of panels) {
    if (!panel || typeof panel !== 'object') continue;
    const key = String(panel.key || '');
    if (!key) continue;
    tab.panels.set(key, panel);
    keyOwner.set(key, tabId);
  }
  // Stamp the tab id here: the content script cannot know its own tab id and
  // the app needs it for every register/hello.
  tab.register = {
    type: 'register',
    tab: tabId,
    page: typeof message.page === 'string' ? message.page : (tab.url || ''),
    panels: panels,
  };
  sendToServer(tab.register);
}

function handleFromContent(tabId, message) {
  if (!message || typeof message !== 'object' || typeof message.type !== 'string') return;
  const tab = ensureTab(tabId);
  switch (message.type) {
    case 'hello':
      if (typeof message.url === 'string' && message.url) tab.url = message.url;
      if (typeof message.title === 'string' && message.title) tab.title = message.title;
      sendToServer({
        type: 'hello',
        version: PROTOCOL_VERSION,
        url: tab.url || '',
        title: tab.title || '',
        tab: tabId,
        frame: 'top',
      });
      // Tell the page where we stand: it may have loaded while the socket was
      // already open and would otherwise never learn that we are connected.
      deliverToTab(tabId, { type: 'mt-conn', connected: socketOpen });
      break;
    case 'register':
      onRegister(tabId, message);
      break;
    case 'request':
      sendToServer({
        type: 'request',
        keys: Array.isArray(message.keys) ? message.keys : [],
        mode: message.mode === 'inplace' ? 'inplace' : 'cards',
        priority: message.priority !== false,
      });
      break;
    case 'cancel':
      sendToServer({ type: 'cancel', keys: Array.isArray(message.keys) ? message.keys : [] });
      break;
    case 'status':
      sendToServer({ type: 'status' });
      break;
    default:
      // Unknown extension-side message: forward verbatim, the app ignores what
      // it does not understand.
      sendToServer(message);
      break;
  }
}

// ------------------------------------------------------------- settings ----

async function applySettings(patch) {
  const next = normalizeSettings({ ...settings, ...patch });
  const portChanged = next.port !== settings.port;
  const changed = next.port !== settings.port || next.mode !== settings.mode || next.enabled !== settings.enabled;
  settings = next;
  if (portChanged) {
    // Point the socket at the new port straight away.
    closeSocket();
    backoffMs = BACKOFF_START_MS;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    ensureConnected();
  }
  if (changed) {
    broadcastToContent({ type: 'mt-settings', settings: publicSettings() });
    broadcastState();
  }
}

async function setEnabled(value) {
  await storageSet({ enabled: !!value });
  await applySettings({ enabled: !!value });
}

function activeTab() {
  return new Promise((resolve) => {
    try {
      chrome.tabs.query({ active: true, currentWindow: true }, (list) => {
        void chrome.runtime.lastError;
        resolve(Array.isArray(list) && list.length ? list[0] : null);
      });
    } catch (err) {
      resolve(null);
    }
  });
}

async function toggleTranslation() {
  const enabled = !settings.enabled;
  await storageSet({ enabled });
  await applySettings({ enabled });
  const tab = await activeTab();
  if (tab && tab.id != null) deliverToTab(tab.id, { type: 'mt-settings', settings: publicSettings() });
  log('translation', enabled ? 'enabled' : 'disabled');
}

// --------------------------------------------------------------- events ----

chrome.runtime.onConnect.addListener((port) => {
  try {
    if (!port || port.name !== 'mt') return;
    const tabId = port.sender && port.sender.tab ? port.sender.tab.id : null;
    if (tabId == null) return;
    contentPorts.set(tabId, port);
    const tab = ensureTab(tabId);
    if (port.sender.tab.url) tab.url = port.sender.tab.url;
    if (port.sender.tab.title) tab.title = port.sender.tab.title;

    port.onMessage.addListener((message) => {
      try {
        if (message && message.to === 'server' && message.msg) handleFromContent(tabId, message.msg);
      } catch (err) {
        warn('content message failed', err);
      }
    });
    port.onDisconnect.addListener(() => {
      void chrome.runtime.lastError;
      contentPorts.delete(tabId);
      // The document is gone (navigation/close) unless the worker was merely
      // restarted — either way the panel registry for this tab is stale.
      forgetTab(tabId);
    });

    // The page just (re)connected: ask it for a fresh register so a restarted
    // worker does not lose the session.
    deliverToTab(tabId, { type: 'mt-need-register' });
  } catch (err) {
    warn('onConnect failed', err);
  }
});

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  try {
    if (!message || typeof message !== 'object') return;
    switch (message.type) {
      case 'mt-get-state':
        respond(publicState());
        return;
      case 'mt-set-enabled':
        void setEnabled(!!message.value);
        respond({ ok: true });
        return;
      case 'mt-set-mode':
        void (async () => {
          const mode = message.value === 'inplace' ? 'inplace' : 'cards';
          await storageSet({ mode });
          await applySettings({ mode });
        })();
        respond({ ok: true });
        return;
      case 'mt-set-port': {
        const port = Number.parseInt(message.value, 10);
        const valid = Number.isInteger(port) && port > 0 && port <= 65535;
        if (valid) {
          void (async () => {
            await storageSet({ port });
            await applySettings({ port });
          })();
        }
        respond({ ok: valid, port: valid ? port : settings.port });
        return;
      }
      case 'mt-reconnect':
        closeSocket();
        backoffMs = BACKOFF_START_MS;
        ensureConnected();
        respond({ ok: true });
        return;
      case 'mt-toggle':
        void toggleTranslation();
        respond({ ok: true });
        return;
      default:
        return;
    }
  } catch (err) {
    warn('onMessage failed', err);
  }
});

chrome.storage.onChanged.addListener((changes, area) => {
  try {
    if (area !== 'local' || !changes) return;
    const patch = {};
    let touched = false;
    if (changes.port) {
      patch.port = changes.port.newValue;
      touched = true;
    }
    if (changes.mode) {
      patch.mode = changes.mode.newValue;
      touched = true;
    }
    if (changes.enabled) {
      patch.enabled = changes.enabled.newValue;
      touched = true;
    }
    if (touched) void applySettings(patch);
  } catch (err) {
    warn('storage.onChanged failed', err);
  }
});

chrome.commands.onCommand.addListener((command) => {
  try {
    if (command === 'toggle-translation') void toggleTranslation();
  } catch (err) {
    warn('onCommand failed', err);
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  try {
    contentPorts.delete(tabId);
    forgetTab(tabId);
  } catch (err) {
    warn('onRemoved failed', err);
  }
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  try {
    if (changeInfo && changeInfo.status === 'loading') {
      // New document: the previous panel list is meaningless.
      forgetTab(tabId);
    } else {
      const tabState = tabs.get(tabId);
      if (tabState) {
        if (tab && tab.url) tabState.url = tab.url;
        if (tab && tab.title) tabState.title = tab.title;
      }
    }
    if (changeInfo && changeInfo.status === 'complete' && contentPorts.has(tabId)) {
      // Safety net: the page is ready, make it re-register its panels.
      deliverToTab(tabId, { type: 'mt-need-register' });
    }
  } catch (err) {
    warn('onUpdated failed', err);
  }
});

chrome.alarms.onAlarm.addListener((alarm) => {
  try {
    if (!alarm || alarm.name !== KEEPALIVE_ALARM) return;
    if (socket && socket.readyState === WebSocket.OPEN) {
      // Cheap traffic keeps the worker (and the app's read loop) alive.
      sendToServer({ type: 'status' });
    } else {
      ensureConnected();
    }
  } catch (err) {
    warn('onAlarm failed', err);
  }
});

// -------------------------------------------------------------- bootstrap --

function bootstrap() {
  if (booted) return;
  booted = true;
  try {
    chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: KEEPALIVE_MINUTES });
  } catch (err) {
    warn('alarms.create failed', err);
  }
  void storageGet(DEFAULTS)
    .then((raw) => {
      settings = normalizeSettings(raw);
      ensureConnected();
      broadcastState();
    })
    .catch((err) => {
      warn('bootstrap failed', err);
      ensureConnected();
    });
}

bootstrap();
