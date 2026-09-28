#!/usr/bin/env node
/**
 * Runtime smoke test for content.js — dev harness, not part of the extension.
 *
 * Node has no browser, so this file builds a deliberately small DOM stub (just
 * the surface content.js touches) and runs the real, unmodified content script
 * against it. It asserts the invariants a browser test would otherwise have to
 * catch by hand:
 *
 *   - discovery filters, dHash keys and the URL fallback for tainted pixels
 *   - register is sent once per change and always carries the full panel list
 *   - cards are created incrementally: existing elements are reused, never rebuilt
 *   - percentage placement, sfx/empty blocks skipped
 *   - in-place payloads reuse the object URL for identical bytes (no blinking)
 *   - disabling restores the page (holder gone, parent position restored, wrap removed)
 *
 * Usage:  node extension/tools/smoke.mjs
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(root, 'content.js'), 'utf8');

// --------------------------------------------------------------- DOM stub ---

const failures = [];
const passes = [];

function describe(value) {
  try {
    return JSON.stringify(value);
  } catch (err) {
    return Object.prototype.toString.call(value);
  }
}

function ok(name, condition, extra) {
  if (condition) passes.push(name);
  else failures.push(name + (extra ? ` — ${extra}` : ''));
}

function eq(name, actual, expected) {
  ok(name, actual === expected, `expected ${describe(expected)}, got ${describe(actual)}`);
}

function makeStyle() {
  const style = {
    removeProperty(name) {
      delete style[name];
      if (name === 'position') delete style.position;
    },
    get cssText() {
      return Object.keys(style)
        .filter((k) => k !== 'cssText' && typeof style[k] === 'string')
        .map((k) => `${k.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase())}:${style[k]}`)
        .join(';');
    },
    set cssText(value) {
      for (const key of Object.keys(style)) {
        if (key !== 'cssText' && typeof style[key] === 'string') delete style[key];
      }
      for (const part of String(value).split(';')) {
        const index = part.indexOf(':');
        if (index <= 0) continue;
        const prop = part.slice(0, index).trim();
        const val = part.slice(index + 1).trim();
        const camel = prop.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
        style[camel] = val;
      }
    },
  };
  return style;
}

const allElements = [];

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.nodeType = 1;
    this.children = [];
    this.parentElement = null;
    this.style = makeStyle();
    this.computed = {};            // test-provided computed style overrides
    this.attrs = new Map();
    this.classes = new Set();
    this.text = '';
    this.rect = { top: 0, left: 0, bottom: 0, right: 0, width: 0, height: 0 };
    this.naturalWidth = 0;
    this.naturalHeight = 0;
    this.src = '';
    this.currentSrc = '';
    this.clientWidth = 0;
    this.clientHeight = 0;
    this.boxless = false;          // true simulates display:none ancestors
    this.tainted = false;          // true simulates cross-origin pixels
    this.id = '';
    allElements.push(this);
    if (this.tagName === 'CANVAS') {
      this.width = 0;
      this.height = 0;
      this.getContext = () => ({
        drawImage: (drawable) => {
          if (!drawable) throw new Error('nothing to draw');
          this._drawn = drawable;
        },
        getImageData: (x, y, w, h) => {
          const drawn = this._drawn;
          if (!drawn || drawn.tainted) throw new Error('SecurityError: tainted canvas');
          let seed = 0;
          // Keyed by path only, so the stub hash behaves like a real perceptual
          // hash: stable across ?sign= and w=720 / w=1280 churn.
          const key = String(drawn.currentSrc || drawn.src || 'x').split('?')[0];
          for (let i = 0; i < key.length; i++) seed = (seed * 31 + key.charCodeAt(i)) >>> 0;
          const data = new Uint8ClampedArray(w * h * 4);
          for (let i = 0; i < w * h; i++) {
            seed = (seed * 1103515245 + 12345) >>> 0;
            const v = (seed >>> 16) & 0xff;
            data[i * 4] = v;
            data[i * 4 + 1] = (v * 7) & 0xff;
            data[i * 4 + 2] = (v * 13) & 0xff;
            data[i * 4 + 3] = 255;
          }
          return { data };
        },
      });
    }
  }

  // --- identity -------------------------------------------------------------
  get isConnected() {
    let node = this;
    while (node.parentElement) node = node.parentElement;
    return node === doc.body || node === doc.documentElement;
  }

  get className() {
    return Array.from(this.classes).join(' ');
  }

  set className(value) {
    this.classes = new Set(String(value).split(/\s+/).filter(Boolean));
  }

  get classList() {
    const classes = this.classes;
    return {
      add: (...names) => names.forEach((n) => classes.add(n)),
      remove: (...names) => names.forEach((n) => classes.delete(n)),
      contains: (name) => classes.has(name),
    };
  }

  get textContent() {
    return this.text;
  }

  set textContent(value) {
    this.text = String(value);
  }

  // --- attributes -----------------------------------------------------------
  setAttribute(name, value) {
    this.attrs.set(name, String(value));
    if (name === 'class') this.className = value;
    if (name === 'src') this.src = String(value);
  }

  getAttribute(name) {
    return this.attrs.has(name) ? this.attrs.get(name) : null;
  }

  hasAttribute(name) {
    return this.attrs.has(name);
  }

  removeAttribute(name) {
    this.attrs.delete(name);
  }

  // --- tree -----------------------------------------------------------------
  appendChild(child) {
    if (child.parentElement) child.parentElement.removeChild(child);
    this.children.push(child);
    child.parentElement = this;
    return child;
  }

  insertBefore(child, ref) {
    if (child.parentElement) child.parentElement.removeChild(child);
    const index = this.children.indexOf(ref);
    if (index < 0) this.children.push(child);
    else this.children.splice(index, 0, child);
    child.parentElement = this;
    return child;
  }

  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    child.parentElement = null;
    return child;
  }

  remove() {
    if (this.parentElement) this.parentElement.removeChild(this);
  }

  contains(node) {
    for (let n = node; n; n = n.parentElement) if (n === this) return true;
    return false;
  }

  getBoundingClientRect() {
    return { ...this.rect };
  }

  getClientRects() {
    return this.boxless ? [] : [this.rect];
  }
}

const documentElement = new El('html');
const body = new El('body');

const doc = {
  documentElement,
  body,
  title: 'Тестовая глава',
  hidden: false,
  listeners: new Map(),
  createElement: (tag) => new El(tag),
  addEventListener(type, fn) {
    if (!doc.listeners.has(type)) doc.listeners.set(type, []);
    doc.listeners.get(type).push(fn);
  },
  fire(type) {
    for (const fn of doc.listeners.get(type) || []) fn({ type });
  },
  get images() {
    return allElements.filter((el) => el.tagName === 'IMG');
  },
};

function getComputedStyle(el) {
  const c = el.computed || {};
  return {
    display: el.style.display || c.display || 'block',
    visibility: el.style.visibility || c.visibility || 'visible',
    opacity: el.style.opacity || c.opacity || '1',
    position: el.style.position || c.position || 'static',
    cssFloat: el.style.cssFloat || c.cssFloat || 'none',
  };
}

// --- message plumbing -------------------------------------------------------

const outbox = [];
const portListeners = [];
const fakePort = {
  postMessage: (message) => outbox.push(message),
  onMessage: { addListener: (fn) => portListeners.push(fn) },
  onDisconnect: { addListener: () => {} },
};

const runtime = {
  id: 'smoke-test',
  lastError: null,
  connect: () => fakePort,
  sendMessage: () => {},
  onMessage: { addListener: () => {} },
};

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  atob,
  Blob,
  URL,
  Uint8Array,
  Uint8ClampedArray,
  Float32Array,
  Promise,
  Date,
  Math,
  Number,
  String,
  Array,
  Object,
  Set,
  Map,
  WeakMap,
  JSON,
  Error,
  chrome: {
    runtime,
    storage: {
      local: { get: (defaults, cb) => cb({ ...defaults }) },
      onChanged: { addListener: () => {} },
    },
  },
  document: doc,
  getComputedStyle,
  MutationObserver: class {
    observe() {}
    disconnect() {}
  },
  location: { href: 'https://reader.test/chapter/1' },
  innerHeight: 800,
  innerWidth: 1200,
  scrollX: 0,
  scrollY: 0,
  addEventListener: () => {},
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'content.js' });

// --- helpers ----------------------------------------------------------------

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const serverMessages = () => outbox.filter((m) => m && m.to === 'server').map((m) => m.msg);
const registers = () => serverMessages().filter((m) => m.type === 'register');
const toServer = (message) => portListeners.forEach((fn) => fn(message));

/** Wait long enough for a throttled refresh (400 ms) to run. */
async function settle() {
  await sleep(520);
}

function addImage(src, { top = 0, parent = null, natural = 1000, tainted = false } = {}) {
  const img = new El('img');
  img.src = src;
  img.currentSrc = src;
  img.naturalWidth = natural;
  img.naturalHeight = natural;
  img.clientWidth = 800;
  img.clientHeight = 800;
  img.rect = { top, left: 0, bottom: top + 800, right: 800, width: 800, height: 800 };
  img.tainted = tainted;
  (parent || body).appendChild(img);
  // Re-run the layout-dependent values now that the node is in the tree.
  img.clientWidth = 800;
  return img;
}

function holderOf(img) {
  for (const el of allElements) {
    if (!el.className.includes('mt-holder')) continue;
    if (el.parentElement && el.parentElement.contains(img)) return el;
  }
  return null;
}

function wrapOf(img) {
  for (const el of allElements) {
    if (el.className.includes('mt-wrap') && el.isConnected && el.contains(img)) return el;
  }
  return null;
}

function addParent(width) {
  const parent = new El('div');
  parent.clientWidth = width;
  parent.clientHeight = 800;
  parent.rect = { top: 0, left: 0, bottom: 800, right: width, width, height: 800 };
  body.appendChild(parent);
  return parent;
}

// ------------------------------------------------------------------- tests --

async function main() {
  await sleep(30); // let init() finish

  // 1. discovery, keys, register -------------------------------------------
  // Picture 1 lives in a tight wrapper, picture 2 inside the body: those are the
  // two anchoring paths (reuse the parent / insert our own wrapper).
  const tightParent = addParent(800);
  const first = addImage('https://cdn.test/pages/01.jpg?sign=aaa', { parent: tightParent });
  const second = addImage('https://cdn.test/pages/02.jpg?sign=bbb', { top: 900 });
  const small = addImage('https://cdn.test/icon.png', { top: 20, natural: 32 });
  const tainted = addImage('https://other.test/pages/03.jpg?w=720', { top: 1800, tainted: true });

  doc.fire('scroll');
  await settle();

  const firstRegister = registers().at(-1);
  ok('register was sent', !!firstRegister);
  const panels = firstRegister ? firstRegister.panels : [];
  eq('only page-sized images are registered', panels.length, 3);
  ok('small decorative image is skipped', !panels.some((p) => p.url.includes('icon.png')));
  ok('panels keep document order', panels[0].url.includes('01.jpg') && panels[1].url.includes('02.jpg'));
  ok('order is 0-based and sequential', panels.map((p) => p.order).join(',') === '0,1,2', panels.map((p) => p.order).join(','));
  ok('pixel hash produces 16 hex chars', /^[0-9a-f]{16}$/.test(panels[0].key), panels[0].key);
  ok('tainted pixels fall back to a query-less URL key',
    panels[2].key === 'u:https://other.test/pages/03.jpg', panels[2].key);
  ok('register carries page url, size, visibility and doc_y',
    firstRegister.page === 'https://reader.test/chapter/1' &&
    panels[0].width === 1000 && panels[0].visible === true && panels[0].doc_y === 0);
  ok('the off-screen panel is marked invisible', panels[2].visible === false);

  const requestAfterRegister = serverMessages().filter((m) => m.type === 'request').at(-1);
  ok('visible panels are requested immediately',
    requestAfterRegister && requestAfterRegister.keys.length === 2 && requestAfterRegister.mode === 'cards' &&
    requestAfterRegister.priority === true);

  // 2. unchanged refresh must not re-send register -------------------------
  const registerCount = registers().length;
  doc.fire('scroll');
  await settle();
  eq('register is not repeated when nothing changed', registers().length, registerCount);

  // 3. cards: creation, skipping sfx/empty, incremental update --------------
  const firstKey = panels[0].key;
  toServer({
    type: 'panel',
    key: firstKey,
    image_w: 1000,
    image_h: 1000,
    blocks: [
      { id: '1:1', x: 100, y: 200, w: 200, h: 80, translation: 'Привет', sfx: false },
      { id: '1:2', x: 500, y: 20, w: 100, h: 40, translation: 'БАМ', sfx: true },
      { id: '1:3', x: 10, y: 900, w: 100, h: 40, translation: '   ', sfx: false },
    ],
    from_cache: false,
    inplace: null,
    error: '',
  });

  const holder = holderOf(first);
  ok('a holder was created for the panel', !!holder);
  const cards = holder ? holder.children.filter((c) => c.className.includes('mt-card')) : [];
  eq('only the real dialogue block becomes a card', cards.length, 1);

  const card = cards[0];
  eq('card text uses textContent', card && card.textContent, 'Привет');
  eq('card left is a percentage of the image width', card && card.style.left, '10%');
  eq('card top is a percentage of the image height', card && card.style.top, '20%');
  eq('card sits above the bubble', card && card.style.transform, 'translateY(-100%) translateY(-6px)');
  eq('card max-width is 1.6x the bubble width', card && card.style.maxWidth, '32%');

  // A block near the top edge must flip below the bubble.
  toServer({
    type: 'panel',
    key: firstKey,
    image_w: 1000,
    image_h: 1000,
    blocks: [
      { id: '1:1', x: 100, y: 200, w: 200, h: 80, translation: 'Привет', sfx: false },
      { id: '2:1', x: 0, y: 10, w: 300, h: 60, translation: 'Сверху нет места', sfx: false },
    ],
    inplace: null,
    error: '',
  });
  const cardsNow = holder.children.filter((c) => c.className.includes('mt-card'));
  eq('a second block adds a card', cardsNow.length, 2);
  const sameCard = cardsNow.find((c) => c.getAttribute('data-mt-id') === '1:1');
  ok('the existing card element is reused, not recreated', sameCard === card);
  const belowCard = cardsNow.find((c) => c.getAttribute('data-mt-id') === '2:1');
  eq('a block at the very top is placed below the bubble', belowCard.style.transform, 'none');
  eq('the flipped card starts at the bubble bottom', belowCard.style.top, '7%');

  // 4. text updates and card removal are incremental -----------------------
  toServer({
    type: 'panel',
    key: firstKey,
    image_w: 1000,
    image_h: 1000,
    blocks: [{ id: '1:1', x: 100, y: 200, w: 200, h: 80, translation: 'Привет!', sfx: false }],
    inplace: null,
    error: '',
  });
  const cardsFinal = holder.children.filter((c) => c.className.includes('mt-card'));
  eq('cards whose ids disappeared are removed', cardsFinal.length, 1);
  ok('the surviving card is still the same element', cardsFinal[0] === card);
  eq('translation text is updated in place', card.textContent, 'Привет!');

  // 5. the host page was not disturbed -------------------------------------
  ok('no wrapper is added when the parent is already tight', !wrapOf(first));
  const anchorPos = tightParent.getAttribute('data-mt-prev-pos');
  eq('the tight parent remembers its original inline position', anchorPos, '');
  ok('the holder lives inside that parent', holder.parentElement === tightParent);
  eq('the parent becomes a positioning context', tightParent.style.position, 'relative');

  // 5b. an image without a tight parent gets our own single wrapper ---------
  const secondKey = panels[1].key;
  toServer({
    type: 'panel',
    key: secondKey,
    image_w: 1000,
    image_h: 1000,
    blocks: [{ id: '9:1', x: 10, y: 300, w: 100, h: 50, translation: 'Второй', sfx: false }],
    inplace: null,
    error: '',
  });
  const wrap = wrapOf(second);
  ok('an image without a tight parent is wrapped', !!wrap && wrap.className.includes('mt-wrap'));
  eq('the wrapper establishes the containing block', wrap.style.position, 'relative');
  eq('the wrapper is exactly the image box', wrap.style.display, 'block');
  ok('the wrapper holds the image and the holder',
    wrap.contains(second) && wrap.contains(holderOf(second)));
  ok('the wrapper is inserted where the image was', body.children.includes(wrap));

  // 5c. a rejected anchor must not wrap anything ---------------------------
  // (floated images are registered but deliberately not drawn)
  const floated = addImage('https://cdn.test/pages/04.jpg', { top: 2400 });
  floated.computed.cssFloat = 'left';
  doc.fire('scroll');
  await settle();
  ok('a floated image is registered but never wrapped',
    !wrapOf(floated) && registers().at(-1).panels.some((p) => p.url.includes('04.jpg')));

  // 6. in-place mode --------------------------------------------------------
  toServer({ type: 'mt-settings', settings: { mode: 'inplace' } });
  const payload = Buffer.from('fake-jpeg-bytes').toString('base64');
  toServer({
    type: 'panel',
    key: firstKey,
    image_w: 1000,
    image_h: 1000,
    blocks: [{ id: '1:1', x: 100, y: 200, w: 200, h: 80, translation: 'Привет!', sfx: false }],
    inplace: { mime: 'image/jpeg', data: payload },
  });
  const inplace = holder.children.find((c) => c.className.includes('mt-inplace'));
  ok('an in-place image is rendered', !!inplace);
  ok('cards are dropped when the artwork is replaced',
    holder.children.filter((c) => c.className.includes('mt-card')).length === 0);
  const firstSrc = inplace && inplace.src;
  ok('the in-place image uses an object URL', typeof firstSrc === 'string' && firstSrc.startsWith('blob:'), String(firstSrc));

  toServer({
    type: 'panel',
    key: firstKey,
    image_w: 1000,
    image_h: 1000,
    blocks: [],
    inplace: { mime: 'image/jpeg', data: payload },
  });
  eq('an identical payload does not recreate the object URL (no blinking)', inplace.src, firstSrc);

  const otherPayload = Buffer.from('other-bytes').toString('base64');
  toServer({
    type: 'panel',
    key: firstKey,
    image_w: 1000,
    image_h: 1000,
    blocks: [],
    inplace: { mime: 'image/jpeg', data: otherPayload },
  });
  ok('a different payload does update the image', inplace.src !== firstSrc);

  // 7. an unknown key never renders and is cancelled -----------------------
  const beforeUnknown = holder.children.length;
  toServer({ type: 'panel', key: 'deadbeefdeadbeef', image_w: 10, image_h: 10, blocks: [{ id: 'x', x: 1, y: 1, w: 1, h: 1, translation: 'нет' }] });
  eq('a panel for an untracked key renders nothing', holder.children.length, beforeUnknown);
  ok('the app is told to cancel the untracked key',
    serverMessages().some((m) => m.type === 'cancel' && m.keys.includes('deadbeefdeadbeef')));

  // 7b. back to cards, then the reader swaps the <img> node ----------------
  toServer({ type: 'mt-settings', settings: { mode: 'cards' } });
  toServer({
    type: 'panel',
    key: firstKey,
    image_w: 1000,
    image_h: 1000,
    blocks: [{ id: '1:1', x: 100, y: 200, w: 200, h: 80, translation: 'Привет!', sfx: false }],
    inplace: null,
  });
  const cardBefore = holder.children.find((c) => c.getAttribute('data-mt-id') === '1:1');
  ok('cards come back when leaving in-place mode', !!cardBefore);

  const replacement = new El('img');
  replacement.src = first.src;
  replacement.currentSrc = first.src;
  replacement.naturalWidth = 1000;
  replacement.naturalHeight = 1000;
  replacement.clientWidth = 800;
  replacement.clientHeight = 800;
  replacement.rect = { ...first.rect };
  tightParent.insertBefore(replacement, first);
  first.remove();
  doc.fire('scroll');
  await settle();
  ok('the holder survives an <img> node replacement', holderOf(replacement) === holder);
  ok('the cards survive an <img> node replacement', holder.children.includes(cardBefore));

  // 7c. the reader rebuilds the whole container ---------------------------
  const rebuiltParent = addParent(800);
  const rebuiltImg = addImage('https://cdn.test/pages/01.jpg?sign=aaa', { parent: rebuiltParent });
  tightParent.remove();
  doc.fire('scroll');
  await settle();
  ok('the very same holder is re-anchored into the rebuilt container', holderOf(rebuiltImg) === holder);
  eq('the new container becomes the anchor', holder.parentElement, rebuiltParent);
  ok('cards are still the same elements after re-anchoring', holder.children.includes(cardBefore));

  // 7d. a resolution swap on the same node keeps the identity -------------
  const registersBeforeSwap = registers().length;
  const signBeforeSwap = registers().at(-1).panels.map((p) => p.key).join(',');
  rebuiltImg.src = 'https://cdn.test/pages/01.jpg?sign=zzz&w=1280';
  rebuiltImg.currentSrc = rebuiltImg.src;
  doc.fire('scroll');
  await settle();
  const afterSwap = registers().at(-1);
  ok('a changed image url is re-registered', registers().length > registersBeforeSwap);
  ok('the app receives the new url',
    afterSwap.panels.some((p) => p.url.includes('w=1280')),
    afterSwap.panels.map((p) => p.url).join(' '));
  eq('a matching dHash keeps the panel key stable', afterSwap.panels.map((p) => p.key).join(','), signBeforeSwap);
  ok('the overlay is untouched by the resolution swap', holderOf(rebuiltImg) === holder && holder.children.includes(cardBefore));

  // 7e. repeated refreshes never re-wrap or rebuild anything ---------------
  const wrapCountBefore = allElements.filter((el) => el.className.includes('mt-wrap') && el.isConnected).length;
  const childCountBefore = holder.children.length;
  doc.fire('scroll');
  await settle();
  doc.fire('scroll');
  await settle();
  eq('refreshes do not add or remove wrappers',
    allElements.filter((el) => el.className.includes('mt-wrap') && el.isConnected).length, wrapCountBefore);
  eq('refreshes never rebuild the card set', holder.children.length, childCountBefore);

  // 8. disabling restores the page ----------------------------------------
  toServer({ type: 'mt-settings', settings: { enabled: false } });
  eq('the holder is removed on disable', holderOf(rebuiltImg), null);
  ok('anchor bookkeeping is cleaned up on every anchor',
    !tightParent.hasAttribute('data-mt-prev-pos') && !rebuiltParent.hasAttribute('data-mt-prev-pos'));
  ok('the anchor inline position is restored', rebuiltParent.style.position === undefined);
  ok('our wrapper is removed and the image is back where it was',
    !wrapOf(second) && second.parentElement === body);
  ok('the badge is removed on disable', !allElements.some((el) => el.className.includes('mt-badge') && el.isConnected));
  const cancelAll = serverMessages().filter((m) => m.type === 'cancel').at(-1);
  ok('every tracked panel is cancelled on disable',
    !!cancelAll && cancelAll.keys.includes(firstKey) && cancelAll.keys.includes(secondKey),
    cancelAll ? cancelAll.keys.join(',') : 'no cancel message');

  // ------------------------------------------------------------- reporting --
  for (const name of passes) console.log('PASS  ' + name);
  for (const name of failures) console.log('FAIL  ' + name);
  console.log('');
  console.log(`${passes.length}/${passes.length + failures.length} runtime checks passed`);
  process.exit(failures.length ? 1 : 0);
}

main().catch((err) => {
  console.error('harness error:', err);
  process.exit(1);
});
