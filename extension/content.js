/**
 * MangaTranslator Bridge — content script.
 *
 * Finds the page images (manga pages), hashes them so the local app can identify
 * the artwork independently of the URL, tells the app what is currently on the
 * page and renders the translations back on top of the images.
 *
 * Why the rendering does not flicker:
 *   - one absolutely positioned holder per panel, sized 100% of the element that
 *     also contains the <img>, so percentage card coordinates follow the image
 *     through scroll, zoom, resize and lazy-load src swaps with zero JS work;
 *   - a card element is created once and afterwards only ever updated in place
 *     (textContent and style writes are skipped when the value is unchanged);
 *   - nothing is ever rebuilt on scroll, and holders are not removed merely
 *     because a panel scrolled out of view.
 */
(() => {
  'use strict';

  // Some pages inject content scripts twice; never build the overlay twice.
  if (window.__mtBridgeLoaded) return;
  window.__mtBridgeLoaded = true;

  const TAG = '[mt]';

  // --- tuning ---------------------------------------------------------------
  const REFRESH_MS = 400;        // all discovery triggers are throttled to this
  const MISSING_GRACE_MS = 2000; // keep a panel this long after its image left the DOM
  const MIN_NATURAL = 500;       // smallest natural side accepted as a page image
  const HASH_W = 9;              // dHash grid: 9x8 comparisons -> 64 bits
  const HASH_H = 8;
  const FADE_MS = 400;           // how long the "fade in" trigger class stays on a new card
  const ERR_CARD_ID = '__mt_err__';

  // Inline geometry of our own nodes. The anchoring contract (holder is
  // position:absolute at 0/0 sized 100%, cards are translated by -100%) must
  // survive a page that blocks or overrides the injected stylesheet.
  const HOLDER_CSS = 'position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none;overflow:visible;';
  const INPLACE_CSS = 'position:absolute;left:0;top:0;width:100%;height:100%;object-fit:fill;pointer-events:none;';
  // The wrapper must establish the containing block for the holder and must be
  // exactly as tall as the image (zeroed line box), or percentage placement drifts.
  const WRAP_CSS = 'position:relative;display:block;line-height:0;font-size:0;';

  const DEFAULTS = { enabled: true, mode: 'cards', port: 8770 };

  // --- state ----------------------------------------------------------------
  const settings = { ...DEFAULTS };
  let port = null;                     // chrome.runtime.Port to the worker
  let portRetry = 0;
  const panels = new Map();            // key -> panel state (image + overlay nodes)
  const panelData = new Map();         // key -> last "panel" message from the app
  const requestedMode = new Map();     // key -> mode we last asked the app for
  const keyByImg = new WeakMap();      // HTMLImageElement -> {src, key}
  const keyByUrl = new Map();          // src -> key (fast path for repeated URLs)
  let lastSignature = '';              // register is only sent when this changes
  let pendingForce = false;            // next refresh must send register anyway
  let refreshGen = 0;                  // discards the results of stale async passes
  let throttleTimer = null;
  let lastRefreshAt = 0;
  let cleanupTimer = null;
  let observedRoot = null;
  let observer = null;
  let badgeEl = null;
  let lastProgress = null;
  let connected = false;
  let hashFallbackLogged = false;

  // --- tiny helpers ---------------------------------------------------------

  function log(...args) {
    console.log(TAG, ...args);
  }

  function warn(...args) {
    console.warn(TAG, ...args);
  }

  /** Write a style only when it actually changes: identical writes dirty the layout. */
  function setStyle(el, prop, value) {
    if (el.style[prop] !== value) el.style[prop] = value;
  }

  /** Run an async mapper over items with a bounded number of parallel tasks. */
  async function mapLimit(items, limit, fn) {
    const out = new Array(items.length);
    let next = 0;
    const workers = [];
    const count = Math.max(1, Math.min(limit, items.length));
    for (let i = 0; i < count; i++) {
      workers.push((async () => {
        for (;;) {
          const index = next++;
          if (index >= items.length) return;
          try {
            out[index] = await fn(items[index], index);
          } catch (err) {
            out[index] = null;
          }
        }
      })());
    }
    await Promise.all(workers);
    return out;
  }

  /** base64 (no data: prefix) -> Blob, used for the in-place replacement image. */
  function base64ToBlob(data, mime) {
    const binary = atob(data);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new Blob([bytes], { type: mime || 'image/png' });
  }

  function currentSrc(img) {
    return img.currentSrc || img.src || '';
  }

  // --- "is this one of our nodes?" -----------------------------------------

  /**
   * Own class/attribute check. Deliberately limited to our generated layers:
   * the site's <img> may legitimately live inside our own .mt-wrap, and its
   * src/style changes must still be observed.
   */
  function isOwnNode(node) {
    if (!node || node.nodeType !== 1) return false;
    const cls = typeof node.className === 'string' ? node.className : '';
    if (cls && /(^|\s)mt-(holder|card|badge|inplace|wrap)(\s|$)/.test(cls)) return true;
    if (typeof node.hasAttribute === 'function') {
      if (node.hasAttribute('data-mt-holder') || node.hasAttribute('data-mt-badge') ||
          node.hasAttribute('data-mt-inplace') || node.hasAttribute('data-mt-wrap')) return true;
    }
    return false;
  }

  /** True for the wrapper we insert around a site image. */
  function isWrapNode(node) {
    if (!node || node.nodeType !== 1) return false;
    const cls = typeof node.className === 'string' ? node.className : '';
    if (cls && /(^|\s)mt-wrap(\s|$)/.test(cls)) return true;
    return typeof node.hasAttribute === 'function' && node.hasAttribute('data-mt-wrap');
  }

  /** True for an <img> that belongs to our overlay rather than to the page. */
  function isOwnOverlay(img) {
    if (isOwnNode(img)) return true;
    // The site's <img> normally lives *inside* our own .mt-wrap, so the wrapper
    // is deliberately skipped here: only our real layers (holder/cards/badge/
    // in-place image) mean "this image is ours". Treating the wrapper as ours
    // would hide the panel we just wrapped and start a wrap/unwrap loop.
    for (let node = img.parentElement, depth = 0; node && depth < 8; node = node.parentElement, depth++) {
      if (isWrapNode(node)) continue;
      if (isOwnNode(node)) return true;
    }
    return false;
  }

  // --- panel discovery ------------------------------------------------------

  /** An image counts as a page when it is big, loaded, rendered and not ours. */
  function isCandidate(img) {
    try {
      if (!img || img.tagName !== 'IMG' || !img.isConnected) return false;
      if (img.naturalWidth < MIN_NATURAL || img.naturalHeight < MIN_NATURAL) return false;
      if (isOwnOverlay(img)) return false;
      const style = getComputedStyle(img);
      if (!style) return false;
      if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
      if (parseFloat(style.opacity) === 0) return false;
      // Covers display:none ancestors, whose computed style on the image itself
      // still looks visible: no box means no layout and no place for a card.
      if (img.getClientRects().length === 0) return false;
      return true;
    } catch (err) {
      return false;
    }
  }

  /** 64-bit perceptual dHash of the current pixels: 16 lowercase hex chars. */
  async function hashImage(img) {
    const canvas = document.createElement('canvas');
    canvas.width = HASH_W;
    canvas.height = HASH_H;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    if (!ctx) return null;
    let bitmap = null;
    try {
      if (typeof createImageBitmap === 'function') bitmap = await createImageBitmap(img);
    } catch (err) {
      bitmap = null; // fall back to drawing the element itself
    }
    try {
      ctx.drawImage(bitmap || img, 0, 0, HASH_W, HASH_H);
    } catch (err) {
      if (bitmap && bitmap.close) bitmap.close();
      return null;
    }
    try {
      // Cross-origin pixels taint the canvas and make this throw — expected on
      // real sites, the caller falls back to a URL derived key.
      const pixels = ctx.getImageData(0, 0, HASH_W, HASH_H).data;
      const gray = new Float32Array(HASH_W * HASH_H);
      for (let i = 0, p = 0; i < gray.length; i++, p += 4) {
        gray[i] = 0.299 * pixels[p] + 0.587 * pixels[p + 1] + 0.114 * pixels[p + 2];
      }
      let hex = '';
      let acc = 0;
      let bits = 0;
      for (let y = 0; y < HASH_H; y++) {
        for (let x = 0; x < HASH_W - 1; x++) {
          const bit = gray[y * HASH_W + x] > gray[y * HASH_W + x + 1] ? 1 : 0;
          acc = (acc << 1) | bit;
          bits++;
          if (bits === 4) {
            hex += acc.toString(16);
            acc = 0;
            bits = 0;
          }
        }
      }
      return hex.length === 16 ? hex : null;
    } catch (err) {
      return null;
    } finally {
      if (bitmap && bitmap.close) bitmap.close();
    }
  }

  /** Fallback identity: the URL without its query string ("w720" / "?sign=" churn). */
  function urlKey(img) {
    const raw = currentSrc(img);
    try {
      const url = new URL(raw, location.href);
      return 'u:' + url.origin + url.pathname;
    } catch (err) {
      return 'u:' + String(raw).split('?')[0];
    }
  }

  /**
   * Stable key of an image. The element cache is src aware: a reader that swaps
   * w720 -> w1280 on the same node must be re-hashed, not silently reused.
   */
  async function keyForImage(img) {
    const src = currentSrc(img);
    const cached = keyByImg.get(img);
    if (cached && cached.src === src) return cached.key;
    const byUrl = src ? keyByUrl.get(src) : null;
    if (byUrl) {
      keyByImg.set(img, { src, key: byUrl });
      return byUrl;
    }
    const hash = await hashImage(img);
    if (!hash && !hashFallbackLogged) {
      hashFallbackLogged = true;
      log('pixel hashing unavailable for some images (cross-origin), using URL keys');
    }
    const key = hash || urlKey(img);
    keyByImg.set(img, { src, key });
    if (src) keyByUrl.set(src, key);
    return key;
  }

  // --- throttled refresh ----------------------------------------------------

  /** Throttle all triggers to at most one refresh per REFRESH_MS (leading + trailing). */
  function scheduleRefresh(force) {
    if (force) pendingForce = true;
    if (!settings.enabled) return;
    const now = Date.now();
    const elapsed = now - lastRefreshAt;
    if (elapsed >= REFRESH_MS) {
      lastRefreshAt = now;
      void refresh('direct').catch((err) => warn('refresh failed', err));
      return;
    }
    if (throttleTimer) return;
    throttleTimer = setTimeout(() => {
      throttleTimer = null;
      lastRefreshAt = Date.now();
      void refresh('trailing').catch((err) => warn('refresh failed', err));
    }, REFRESH_MS - elapsed);
  }

  /** Give a vanished panel a grace period so transient DOM churn cannot flicker it. */
  function scheduleCleanup() {
    if (cleanupTimer) return;
    cleanupTimer = setTimeout(() => {
      cleanupTimer = null;
      scheduleRefresh(false);
    }, MISSING_GRACE_MS + 100);
  }

  /**
   * Full discovery pass: collect images -> keys -> order/visibility -> register
   * -> render -> request. The panel list is always computed as a whole and sent
   * as a whole; the app merges it by key.
   */
  async function refresh(reason) {
    if (!settings.enabled) return;
    const gen = ++refreshGen;
    ensureObserver();

    const candidates = [];
    const images = document.images;
    for (let i = 0; i < images.length; i++) {
      const img = images[i];
      if (isCandidate(img)) candidates.push(img);
    }

    const keys = await mapLimit(candidates, 4, (img) => keyForImage(img));
    // A newer pass started while we were hashing: its result is the fresh one.
    if (gen !== refreshGen || !settings.enabled) return;

    const rows = [];
    for (let i = 0; i < candidates.length; i++) {
      if (!keys[i]) continue;
      rows.push({ img: candidates[i], key: keys[i], rect: candidates[i].getBoundingClientRect() });
    }
    const scrollX = window.scrollX;
    const scrollY = window.scrollY;
    rows.sort((a, b) => (a.rect.top + scrollY) - (b.rect.top + scrollY) ||
                        (a.rect.left + scrollX) - (b.rect.left + scrollX));

    const viewport = window.innerHeight || 0;
    const list = [];
    const seen = new Set();
    let order = 0;
    for (const row of rows) {
      if (seen.has(row.key)) continue; // the same artwork twice on a page: one panel
      seen.add(row.key);
      const img = row.img;
      list.push({
        key: row.key,
        img,
        url: currentSrc(img),
        width: img.naturalWidth,
        height: img.naturalHeight,
        order: order++,
        // Visible = intersects the viewport expanded by one viewport height.
        visible: row.rect.bottom > -viewport && row.rect.top < viewport * 2,
        doc_y: Math.round((row.rect.top + scrollY) * 10) / 10,
      });
    }

    // Reconcile the panel states with what is on the page right now.
    const now = Date.now();
    for (const info of list) {
      let state = panels.get(info.key);
      if (!state) {
        state = {
          key: info.key,
          img: null,
          url: '',
          width: 0,
          height: 0,
          order: info.order,
          visible: info.visible,
          doc_y: info.doc_y,
          missingSince: 0,
          anchorEl: null,
          wrapEl: null,
          holderEl: null,
          cards: new Map(),
          inplaceEl: null,
          inplaceUrl: '',
          inplaceData: null,
          inplaceMime: '',
        };
        panels.set(info.key, state);
      }
      state.order = info.order;
      state.visible = info.visible;
      state.doc_y = info.doc_y;
      state.url = info.url;
      state.width = info.width;
      state.height = info.height;
      state.missingSince = 0;
      if (state.img !== info.img) rebindImage(state, info.img);
    }

    // Artwork that left the page: release after a short grace period.
    const stale = [];
    for (const [key, state] of panels) {
      if (seen.has(key)) continue;
      if (!state.missingSince) state.missingSince = now;
      if (now - state.missingSince >= MISSING_GRACE_MS) stale.push(key);
      else scheduleCleanup();
    }
    for (const key of stale) releasePanel(key);

    // Register only when the key set / order / visibility / URL set changed.
    const signature = list.map((p) => `${p.key}:${p.order}:${p.visible ? 'v' : '-'}:${p.url}`).join('|');
    const force = pendingForce;
    pendingForce = false;
    if (force || signature !== lastSignature) {
      lastSignature = signature;
      sendRegister(list);
    }

    // Render whatever the app already answered for the panels currently known.
    for (const info of list) {
      if (panelData.has(info.key)) renderPanel(info.key);
    }

    requestVisible(false);
    updateBadge();
  }

  /** The reader replaced the <img> node: keep the holder and its cards, re-anchor. */
  function rebindImage(state, img) {
    const previous = state.img;
    state.img = img;
    if (state.wrapEl && !state.wrapEl.isConnected) state.wrapEl = null;
    const holder = state.holderEl;
    // Nothing rendered for this panel yet: the overlay is created lazily, on the
    // first payload, so a page that is never translated is never touched.
    if (!holder) return;
    const host = holder.parentElement;
    // Fast path: the new node landed where the holder already is, so the overlay
    // (and every card in it) simply stays put.
    if (host && host.isConnected && host.contains(img)) return;
    if (previous && previous !== img) log('image node replaced, re-anchoring panel', state.key);
    attachHolder(state);
  }

  // --- anchoring ------------------------------------------------------------

  /** A wrapper is only "tight" when it is basically just the image. */
  function isTightHost(host, imageWidth) {
    if (!host || !host.isConnected) return false;
    if (host.children.length > 3) return false;
    const hostWidth = host.clientWidth;
    if (!imageWidth || !hostWidth) return false;
    return Math.abs(hostWidth - imageWidth) <= imageWidth * 0.25;
  }

  function nearestPositionedAncestor(img) {
    let node = img.parentElement;
    for (let depth = 0; node && depth < 6; node = node.parentElement, depth++) {
      const style = getComputedStyle(node);
      if (style && style.position !== 'static') return node;
    }
    return null;
  }

  /** Remember the inline position so releasing a panel restores the page exactly. */
  function markAnchorPosition(el) {
    if (!el.hasAttribute('data-mt-prev-pos')) {
      el.setAttribute('data-mt-prev-pos', el.style.position || '');
    }
    const style = getComputedStyle(el);
    // Only a static box needs changing: relative with auto offsets never moves
    // anything, while forcing it on a fixed/absolute host could break the site.
    if (!style || style.position === 'static') el.style.position = 'relative';
  }

  function unmarkAnchorPosition(el) {
    if (!el || !el.hasAttribute || !el.hasAttribute('data-mt-prev-pos')) return;
    const previous = el.getAttribute('data-mt-prev-pos') || '';
    el.removeAttribute('data-mt-prev-pos');
    if (previous) el.style.position = previous;
    else el.style.removeProperty('position');
  }

  /**
   * Pick the element the holder lives in. Returns null when the page layout
   * cannot host an overlay without risk (positioned image in an unknown host) —
   * the panel is then still registered, it just renders nothing.
   */
  function chooseAnchor(state, img) {
    const style = getComputedStyle(img);
    const imageWidth = img.clientWidth || Math.round(img.getBoundingClientRect().width) || 0;
    const isPositioned = !!style && (style.position === 'absolute' || style.position === 'fixed');

    if (isPositioned) {
      // Wrapping a positioned image would move its containing block, so reuse an
      // ancestor that is already positioned and leave its styles alone.
      const host = nearestPositionedAncestor(img);
      if (host && host !== document.body && host !== document.documentElement && isTightHost(host, imageWidth)) {
        return { el: host, mark: false };
      }
      return null;
    }

    // Reuse our wrapper while the image is still inside it: that keeps the anchor
    // stable (and never leaves a stray wrapper behind if the layout changes).
    if (state.wrapEl && state.wrapEl.isConnected && state.wrapEl.contains(img)) {
      return { el: state.wrapEl, mark: false };
    }

    // A floated image must not be wrapped: the wrapper's height would collapse
    // around the float and the surrounding layout would break. For those only an
    // already tight host is used, otherwise the panel is registered but not drawn.
    if (style && style.cssFloat && style.cssFloat !== 'none') return null;

    const parent = img.parentElement;
    if (parent && parent !== document.body && parent !== document.documentElement && isTightHost(parent, imageWidth)) {
      return { el: parent, mark: true };
    }

    if (!parent) return null;
    const wrap = document.createElement('div');
    wrap.className = 'mt-wrap';
    wrap.setAttribute('data-mt-wrap', '');
    wrap.style.cssText = WRAP_CSS;
    parent.insertBefore(wrap, img);
    wrap.appendChild(img);
    state.wrapEl = wrap;
    return { el: wrap, mark: false };
  }

  /**
   * Get (or create) the holder and make sure it is anchored to the image's box.
   * Moving an existing holder re-parents it with all of its cards intact.
   */
  function attachHolder(state) {
    const img = state.img;
    if (!img || !img.isConnected) return null;
    let holder = state.holderEl;
    if (!holder) {
      holder = document.createElement('div');
      holder.className = 'mt-holder';
      holder.setAttribute('data-mt-holder', '');
      holder.style.cssText = HOLDER_CSS;
      state.holderEl = holder;
    }
    const host = holder.parentElement;
    if (host && host.isConnected && host.contains(img)) return holder; // already anchored

    // The image left our wrapper: drop the stale wrapper, but only when it holds
    // nothing besides our own holder (never destroy page content).
    if (state.wrapEl && state.wrapEl.isConnected && !state.wrapEl.contains(img)) {
      const foreign = Array.from(state.wrapEl.children).filter((node) => node !== holder);
      if (!foreign.length) {
        state.wrapEl.remove();
        state.wrapEl = null;
      }
    }

    const anchor = chooseAnchor(state, img);
    if (!anchor) return null;
    if (state.anchorEl && state.anchorEl !== anchor.el) unmarkAnchorPosition(state.anchorEl);
    state.anchorEl = anchor.el;
    if (anchor.mark) markAnchorPosition(anchor.el);
    if (holder.parentElement !== anchor.el) anchor.el.appendChild(holder);
    return holder;
  }

  // --- card rendering -------------------------------------------------------

  /** Create/update/remove cards of one panel — never a full rebuild. */
  function renderBlocks(state, holder, data, inplaceShown) {
    if (inplaceShown) {
      clearCards(state); // the replacement image covers the artwork: no cards
      return;
    }
    const blocks = Array.isArray(data.blocks) ? data.blocks : [];
    const wanted = new Map();
    for (const block of blocks) {
      if (!block || typeof block !== 'object') continue;
      const id = block.id == null ? '' : String(block.id);
      if (!id) continue;
      if (block.sfx) continue; // sound effects are not dialogue
      const text = typeof block.translation === 'string' ? block.translation : '';
      if (!text.trim()) continue; // nothing to show for this block
      wanted.set(id, block);
    }
    if (!wanted.size && data.error) wanted.set(ERR_CARD_ID, null);

    // Remove only the cards whose ids disappeared.
    for (const [id, el] of state.cards) {
      if (!wanted.has(id)) {
        el.remove();
        state.cards.delete(id);
      }
    }

    const imageW = Number(data.image_w) || state.width || 0;
    const imageH = Number(data.image_h) || state.height || 0;
    if (!imageW || !imageH) return; // percentages need the image geometry

    for (const [id, block] of wanted) {
      let el = state.cards.get(id);
      const isNew = !el;
      if (isNew) {
        el = document.createElement('div');
        el.className = 'mt-card';
        el.setAttribute('data-mt-id', id);
        holder.appendChild(el);
        state.cards.set(id, el);
      }
      if (block) {
        const text = String(block.translation);
        if (el.textContent !== text) el.textContent = text; // textContent only, never innerHTML
        if (el.classList.contains('mt-err')) el.classList.remove('mt-err');
        placeCard(el, block, imageW, imageH);
      } else {
        const text = 'Ошибка: ' + String(data.error || '');
        if (el.textContent !== text) el.textContent = text;
        el.classList.add('mt-err');
        setStyle(el, 'left', '0%');
        setStyle(el, 'top', '0%');
        setStyle(el, 'maxWidth', '100%');
        setStyle(el, 'transform', 'none');
      }
      if (isNew) fadeIn(el);
    }
  }

  /** Percentage with sane precision: never emit values like 7.000000000000001%. */
  function pct(value) {
    return (Math.round(value * 10000) / 10000) + '%';
  }

  /**
   * Percentage placement: the holder is exactly the image box, so these numbers
   * stay correct through scroll, zoom, resize and lazy-load src swaps.
   */
  function placeCard(el, block, imageW, imageH) {
    const x = Number(block.x) || 0;
    const y = Number(block.y) || 0;
    const w = Number(block.w) || 0;
    const h = Number(block.h) || 0;
    setStyle(el, 'left', pct((x / imageW) * 100));
    // max-width: never narrower than 140px, otherwise 1.6x the bubble width.
    setStyle(el, 'maxWidth', pct(Math.max(140, w * 1.6) / imageW * 100));
    if (y / imageH < 0.04) {
      // No room above the bubble: place the card just below it instead.
      setStyle(el, 'top', pct(((y + h) / imageH) * 100));
      setStyle(el, 'transform', 'none');
    } else {
      // Sit just above the bubble without knowing the card's own height.
      setStyle(el, 'top', pct((y / imageH) * 100));
      setStyle(el, 'transform', 'translateY(-100%) translateY(-6px)');
    }
  }

  /** Fade a brand new card in; the trigger class is dropped so re-attach cannot replay it. */
  function fadeIn(el) {
    el.classList.add('mt-new');
    setTimeout(() => {
      try {
        el.classList.remove('mt-new');
      } catch (err) {
        /* element gone */
      }
    }, FADE_MS);
  }

  function clearCards(state) {
    for (const [, el] of state.cards) el.remove();
    state.cards.clear();
  }

  // --- in-place rendering ---------------------------------------------------

  /** Swap the artwork for the translated image. Returns true when it is shown. */
  function renderInplace(state, holder, data) {
    const payload = data ? data.inplace : null;
    const hasPayload = !!(payload && typeof payload.data === 'string' && payload.data);
    if (!hasPayload || settings.mode !== 'inplace') {
      destroyInplace(state);
      return false;
    }
    let el = state.inplaceEl;
    if (!el) {
      el = document.createElement('img');
      el.className = 'mt-inplace';
      el.setAttribute('data-mt-inplace', '');
      el.setAttribute('alt', '');
      el.style.cssText = INPLACE_CSS;
      state.inplaceEl = el;
    }
    // Re-parent after a DOM swap so the layer stays inside its holder.
    if (el.parentElement !== holder) holder.appendChild(el);
    // String comparison is a value comparison: an identical resend never
    // re-creates the blob, so the visible image never blinks.
    if (state.inplaceData !== payload.data || state.inplaceMime !== (payload.mime || '')) {
      try {
        const blob = base64ToBlob(payload.data, payload.mime);
        const url = URL.createObjectURL(blob);
        const previousUrl = state.inplaceUrl;
        el.src = url; // assign first: the element must never point at a revoked URL
        state.inplaceUrl = url;
        state.inplaceData = payload.data;
        state.inplaceMime = payload.mime || '';
        if (previousUrl) URL.revokeObjectURL(previousUrl);
      } catch (err) {
        warn('bad in-place payload', err);
      }
    }
    setStyle(el, 'display', 'block');
    return true;
  }

  function destroyInplace(state) {
    if (state.inplaceUrl) {
      try {
        URL.revokeObjectURL(state.inplaceUrl);
      } catch (err) {
        /* ignore */
      }
    }
    state.inplaceUrl = '';
    state.inplaceData = null;
    state.inplaceMime = '';
    if (state.inplaceEl) {
      state.inplaceEl.remove();
      state.inplaceEl = null;
    }
  }

  // --- panel lifecycle ------------------------------------------------------

  /** Render one panel from the last payload the app sent for it. */
  function renderPanel(key) {
    try {
      const state = panels.get(key);
      const data = panelData.get(key);
      if (!state || !data) return;
      const holder = attachHolder(state);
      if (!holder) return;
      const inplaceShown = renderInplace(state, holder, data);
      renderBlocks(state, holder, data, inplaceShown);
    } catch (err) {
      warn('render failed for', key, err);
    }
  }

  /** Remove every trace of a panel from the page and restore the layout. */
  function releasePanel(key) {
    const state = panels.get(key);
    if (!state) return;
    try {
      clearCards(state);
      destroyInplace(state);
      if (state.holderEl) {
        state.holderEl.remove();
        state.holderEl = null;
      }
      if (state.wrapEl && state.wrapEl.isConnected) {
        // Put the image back exactly where the wrapper was.
        if (state.img && state.img.isConnected && state.wrapEl.contains(state.img)) {
          const parent = state.wrapEl.parentElement;
          if (parent) parent.insertBefore(state.img, state.wrapEl);
        }
        state.wrapEl.remove();
        state.wrapEl = null;
      }
      unmarkAnchorPosition(state.anchorEl);
      state.anchorEl = null;
    } catch (err) {
      warn('release failed for', key, err);
    }
    panels.delete(key);
    panelData.delete(key);
    requestedMode.delete(key);
  }

  /** Drop everything (translation switched off for this page). */
  function releaseAll() {
    const keys = Array.from(panels.keys());
    if (keys.length) send({ type: 'cancel', keys });
    for (const key of keys) releasePanel(key);
    panels.clear();
    panelData.clear();
    requestedMode.clear();
    lastSignature = '';
    removeBadge();
  }

  // --- messaging ------------------------------------------------------------

  function send(message) {
    const channel = port;
    if (!channel) return false;
    try {
      channel.postMessage({ to: 'server', msg: message });
      return true;
    } catch (err) {
      port = null;
      schedulePortReconnect();
      return false;
    }
  }

  /** The full known list is always sent; the app merges by key. */
  function sendRegister(list) {
    send({
      type: 'register',
      page: location.href,
      // The worker stamps the real tab id — a content script cannot know it.
      panels: list.map((p) => ({
        key: p.key,
        url: p.url,
        width: p.width,
        height: p.height,
        order: p.order,
        visible: p.visible,
        doc_y: p.doc_y,
      })),
    });
  }

  /** Keys the app should work on right now (newly visible panels, mode change). */
  function requestVisible(forceAll) {
    if (!settings.enabled) return;
    const keys = [];
    for (const state of panels.values()) {
      if (!state.visible) continue;
      const data = panelData.get(state.key);
      const asked = requestedMode.get(state.key);
      if (forceAll) {
        keys.push(state.key);
      } else if (!data) {
        if (asked !== settings.mode) keys.push(state.key);
      } else if (data.__mode !== settings.mode && asked !== settings.mode) {
        keys.push(state.key);
      }
    }
    if (!keys.length) return;
    if (!send({ type: 'request', keys, mode: settings.mode, priority: true })) return;
    for (const key of keys) requestedMode.set(key, settings.mode);
  }

  function onPanelMessage(message) {
    const key = typeof message.key === 'string' ? message.key : '';
    if (!key) return;
    const state = panels.get(key);
    if (!state) {
      // The app answered a panel we no longer track (scrolled away, disabled):
      // tell it to stop and ignore the payload.
      send({ type: 'cancel', keys: [key] });
      return;
    }
    // Remember which mode produced this payload so a mode switch re-requests it.
    message.__mode = requestedMode.get(key) || settings.mode;
    requestedMode.delete(key);
    panelData.set(key, message);
    renderPanel(key);
  }

  function onProgress(message) {
    lastProgress = {
      done: Number(message.done) || 0,
      total: Number(message.total) || 0,
      queued: Number(message.queued) || 0,
      failed: Number(message.failed) || 0,
    };
    updateBadge();
  }

  function onPortMessage(message) {
    try {
      if (!message || typeof message !== 'object') return;
      switch (message.type) {
        case 'panel':
          onPanelMessage(message);
          break;
        case 'progress':
          onProgress(message);
          break;
        case 'welcome':
          connected = true;
          updateBadge();
          break;
        case 'error':
          warn('app error:', message.message || '', message.kind || '');
          break;
        case 'status':
          break;
        case 'mt-conn':
          connected = !!message.connected;
          updateBadge();
          break;
        case 'mt-settings':
          applySettings(message.settings);
          break;
        case 'mt-need-register':
          // The worker restarted or the page just finished loading: re-announce.
          lastSignature = '';
          scheduleRefresh(true);
          break;
        default:
          break;
      }
    } catch (err) {
      warn('port message failed', err);
    }
  }

  function schedulePortReconnect() {
    if (!chrome.runtime || !chrome.runtime.id) return; // extension reloaded/disabled
    portRetry = Math.min(portRetry + 1, 6);
    const delay = Math.min(500 * portRetry, 5000);
    setTimeout(connectPort, delay);
  }

  function connectPort() {
    if (port) return;
    try {
      const channel = chrome.runtime.connect({ name: 'mt' });
      port = channel;
      portRetry = 0;
      channel.onMessage.addListener(onPortMessage);
      channel.onDisconnect.addListener(() => {
        void chrome.runtime.lastError;
        port = null;
        connected = false;
        updateBadge();
        schedulePortReconnect();
      });
      send({ type: 'hello', version: 1, url: location.href, title: document.title });
      scheduleRefresh(true);
    } catch (err) {
      port = null;
      warn('connect failed', err);
      schedulePortReconnect();
    }
  }

  // Second delivery path: the background falls back to `tabs.sendMessage` while the
  // port is being re-established (worker restart), and messages sent that way must
  // not be dropped either.
  if (chrome.runtime && chrome.runtime.onMessage) {
    chrome.runtime.onMessage.addListener((message) => {
      onPortMessage(message);
      return false;
    });
  }

  // Read-only snapshot for tools/extension_watch.py. It lives in the content
  // script's isolated world, so a page script cannot see or call it; the tool
  // evaluates it through the CDP context of that world.
  try {
    globalThis.__mtDebug = () => ({
      panels: panels.size,
      payloads: panelData.size,
      port: !!port,
      connected,
      enabled: settings.enabled,
      mode: settings.mode,
      signature: lastSignature ? lastSignature.length : 0,
      badge: badgeEl ? badgeEl.textContent : '',
    });
  } catch (err) {
    warn('debug hook failed', err);
  }

  // --- status badge ---------------------------------------------------------

  function ensureBadge() {
    if (badgeEl && badgeEl.isConnected) return badgeEl;
    const host = document.body || document.documentElement;
    if (!host) return null;
    badgeEl = document.createElement('div');
    badgeEl.className = 'mt-badge';
    badgeEl.setAttribute('data-mt-badge', '');
    badgeEl.textContent = 'Перевод: ожидание';
    host.appendChild(badgeEl);
    return badgeEl;
  }

  function updateBadge() {
    if (!settings.enabled) return;
    const el = ensureBadge();
    if (!el) return;
    let text;
    if (!connected) text = 'Перевод: нет связи';
    else if (lastProgress) text = `Перевод: ${lastProgress.done}/${lastProgress.total}`;
    else text = 'Перевод: ожидание';
    if (el.textContent !== text) el.textContent = text;
  }

  function removeBadge() {
    if (badgeEl) {
      badgeEl.remove();
      badgeEl = null;
    }
  }

  // --- enable / disable / mode ---------------------------------------------

  function applySettings(next) {
    if (!next) return;
    const mode = next.mode === 'inplace' ? 'inplace' : 'cards';
    const enabled = next.enabled !== false;
    const enabledChanged = enabled !== settings.enabled;
    const modeChanged = mode !== settings.mode;
    settings.mode = mode;
    settings.enabled = enabled;

    if (!enabledChanged && !modeChanged) return;
    if (enabledChanged && !enabled) {
      releaseAll();
      return;
    }
    if (enabledChanged && enabled) {
      lastSignature = '';
      updateBadge();
      scheduleRefresh(true);
      return;
    }
    // Mode switch: re-render from the payloads we already have, then ask the app
    // for the same panels in the new mode.
    for (const key of Array.from(panels.keys())) {
      if (panelData.has(key)) renderPanel(key);
    }
    requestVisible(true);
  }

  // --- observers ------------------------------------------------------------

  function trigger() {
    scheduleRefresh(false);
  }

  /** (Re)observe the current body: readers sometimes replace it wholesale. */
  function ensureObserver() {
    if (observer && observedRoot && observedRoot.isConnected && observedRoot === (document.body || document.documentElement)) return;
    const root = document.body || document.documentElement;
    if (!root) return;
    if (observer) observer.disconnect();
    observedRoot = root;
    observer = new MutationObserver((records) => {
      for (const record of records) {
        // Our own nodes change constantly (card styles, in-place images): ignore
        // them so rendering can never feed discovery.
        if (isOwnNode(record.target)) continue;
        trigger();
        return;
      }
    });
    observer.observe(root, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ['src', 'style', 'class'],
    });
  }

  function installListeners() {
    // capture:true also catches scrolling inside the reader's own scroll container.
    document.addEventListener('scroll', trigger, { capture: true, passive: true });
    window.addEventListener('resize', trigger, { passive: true });
    window.addEventListener('pageshow', trigger);
    window.addEventListener('load', trigger);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) trigger();
    });
    if (chrome.storage && chrome.storage.onChanged) {
      chrome.storage.onChanged.addListener((changes, area) => {
        if (area !== 'local' || !changes) return;
        const patch = {};
        if (changes.enabled) patch.enabled = changes.enabled.newValue;
        if (changes.mode) patch.mode = changes.mode.newValue;
        if ('enabled' in patch || 'mode' in patch) applySettings(patch);
      });
    }
  }

  // --- boot -----------------------------------------------------------------

  function loadSettings() {
    return new Promise((resolve) => {
      try {
        chrome.storage.local.get(DEFAULTS, (value) => {
          void chrome.runtime.lastError;
          resolve(value && typeof value === 'object' ? value : { ...DEFAULTS });
        });
      } catch (err) {
        resolve({ ...DEFAULTS });
      }
    });
  }

  async function init() {
    const stored = await loadSettings();
    settings.mode = stored.mode === 'inplace' ? 'inplace' : 'cards';
    settings.enabled = stored.enabled !== false;
    settings.port = Number(stored.port) || DEFAULTS.port;
    installListeners();
    ensureObserver();
    connectPort();
    if (settings.enabled) {
      updateBadge();
      scheduleRefresh(true);
    }
  }

  void init().catch((err) => warn('init failed', err));
})();
