/**
 * MangaTranslator Bridge — popup (Russian UI).
 *
 * The popup holds no state of its own: it asks the service worker for a snapshot
 * and re-renders on every broadcast. When the app is not running the status and
 * progress controls are disabled, but the port field stays editable so the user
 * can point the bridge at a different port.
 */
'use strict';

const els = {
  connLine: document.getElementById('conn-line'),
  dot: document.getElementById('dot'),
  endpoint: document.getElementById('endpoint'),
  tabs: document.getElementById('tabs'),
  provider: document.getElementById('provider'),
  model: document.getElementById('model'),
  target: document.getElementById('target'),
  modeCards: document.getElementById('mode-cards'),
  modeInplace: document.getElementById('mode-inplace'),
  modeHint: document.getElementById('mode-hint'),
  toggle: document.getElementById('toggle'),
  toggleState: document.getElementById('toggle-state'),
  progress: document.getElementById('progress'),
  port: document.getElementById('port'),
  save: document.getElementById('save'),
  portMsg: document.getElementById('port-msg'),
  err: document.getElementById('err'),
};

const MODE_HINT = {
  cards: 'Перевод показывается всплывающими карточками рядом с репликами.',
  inplace: 'Переведённая картинка подменяет оригинал целиком.',
};

let state = null;
let portDirty = false;

/** chrome.runtime.sendMessage with the callback API, so a dead worker cannot throw. */
function ask(message) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(message, (reply) => {
        void chrome.runtime.lastError;
        resolve(reply || null);
      });
    } catch (err) {
      resolve(null);
    }
  });
}

function setText(el, value) {
  const text = value == null ? '' : String(value);
  if (el && el.textContent !== text) el.textContent = text;
}

/** Paint a snapshot; everything is derived, no DOM is rebuilt. */
function render(next) {
  if (next) state = next;
  const s = state;
  if (!s) {
    setText(els.connLine, 'Нет связи с приложением');
    els.dot.className = 'dot dot-off';
    return;
  }

  // --- connection ---
  els.dot.className = s.connected ? 'dot dot-on' : 'dot dot-off';
  setText(els.connLine, s.connected ? 'Подключено' : 'Нет связи с приложением');
  setText(els.endpoint, s.endpoint || `ws://127.0.0.1:${s.port}/ws`);
  setText(els.tabs, s.tabs == null ? '0' : String(s.tabs));

  // --- app info ---
  setText(els.provider, s.provider || '—');
  setText(els.model, s.model || '—');
  setText(els.target, s.targetLanguage || '—');

  // --- mode ---
  const mode = s.mode === 'inplace' ? 'inplace' : 'cards';
  els.modeCards.setAttribute('aria-checked', mode === 'cards' ? 'true' : 'false');
  els.modeInplace.setAttribute('aria-checked', mode === 'inplace' ? 'true' : 'false');
  setText(els.modeHint, MODE_HINT[mode]);

  // --- toggle ---
  els.toggle.setAttribute('aria-pressed', s.enabled ? 'true' : 'false');
  setText(els.toggleState, s.enabled ? 'Состояние: включено' : 'Состояние: выключено');

  // --- progress ---
  const p = s.progress || { done: 0, total: 0 };
  let progressText = `готово ${p.done || 0} из ${p.total || 0}`;
  if (p.queued) progressText += ` · в очереди ${p.queued}`;
  if (p.failed) progressText += ` · ошибок ${p.failed}`;
  setText(els.progress, progressText);

  // --- error line ---
  if (s.error) {
    els.err.hidden = false;
    setText(els.err, 'Ошибка: ' + s.error);
  } else {
    els.err.hidden = true;
    setText(els.err, '');
  }

  // --- controls: everything except the port field needs a live bridge ---
  const live = !!s.connected;
  els.toggle.disabled = !live;
  els.modeCards.disabled = !live;
  els.modeInplace.disabled = !live;

  if (!portDirty && document.activeElement !== els.port) {
    const value = String(s.port);
    if (els.port.value !== value) els.port.value = value;
  }
}

async function refresh() {
  const snapshot = await ask({ type: 'mt-get-state' });
  render(snapshot);
}

function flashPortMessage(text, kind) {
  setText(els.portMsg, text);
  els.portMsg.className = kind ? `hint ${kind}` : 'hint';
  if (text) {
    setTimeout(() => {
      if (els.portMsg.textContent === text) {
        setText(els.portMsg, '');
        els.portMsg.className = 'hint';
      }
    }, 2500);
  }
}

async function savePort() {
  const raw = els.port.value.trim();
  const value = Number.parseInt(raw, 10);
  if (!Number.isInteger(value) || value < 1 || value > 65535) {
    flashPortMessage('Некорректный порт: нужно число от 1 до 65535', 'bad');
    return;
  }
  const reply = await ask({ type: 'mt-set-port', value });
  if (reply && reply.ok) {
    portDirty = false;
    flashPortMessage('Сохранено', 'ok');
    await refresh();
  } else {
    flashPortMessage('Не удалось сохранить порт', 'bad');
  }
}

els.toggle.addEventListener('click', async () => {
  if (!state) return;
  await ask({ type: 'mt-set-enabled', value: !state.enabled });
  await refresh();
});

els.modeCards.addEventListener('click', async () => {
  await ask({ type: 'mt-set-mode', value: 'cards' });
  await refresh();
});

els.modeInplace.addEventListener('click', async () => {
  await ask({ type: 'mt-set-mode', value: 'inplace' });
  await refresh();
});

els.save.addEventListener('click', () => void savePort());

els.port.addEventListener('input', () => {
  portDirty = true;
  setText(els.portMsg, '');
});

els.port.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') void savePort();
});

// Live updates pushed by the worker.
chrome.runtime.onMessage.addListener((message) => {
  if (message && message.type === 'mt-state' && message.state) render(message.state);
});

void refresh();
// Cheap safety net: the popup may have been opened while the worker was asleep.
setInterval(() => void refresh(), 1000);
