#!/usr/bin/env node
/**
 * Static self-check for the MangaTranslator Bridge extension.
 *
 * No dependencies, no browser: it only proves that the manifest is valid, that
 * every referenced file exists, and that a few invariants of the rendering
 * contract are actually present in the sources.
 *
 * Usage:  node extension/tools/selfcheck.mjs
 * Exit code 0 = all checks passed, 1 = at least one failure.
 */
import { readFileSync, existsSync, statSync } from 'node:fs';
import { dirname, resolve, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const results = [];

/** Run one named check; a throw (or a false return) marks it failed. */
function check(name, fn) {
  try {
    const value = fn();
    if (value === false) throw new Error('assertion returned false');
    results.push({ name, ok: true });
  } catch (err) {
    results.push({ name, ok: false, error: err && err.message ? err.message : String(err) });
  }
}

function read(relPath) {
  return readFileSync(join(root, relPath), 'utf8');
}

function assertFile(relPath, why) {
  const full = join(root, relPath);
  if (!existsSync(full)) throw new Error(`missing file ${relPath}${why ? ` (${why})` : ''}`);
  if (!statSync(full).isFile()) throw new Error(`${relPath} is not a file`);
  // An empty referenced file is almost always a mistake.
  if (statSync(full).size === 0) throw new Error(`${relPath} is empty`);
  return full;
}

// --- 1. the manifest parses as JSON ----------------------------------------
let manifest = null;
check('manifest.json parses as JSON', () => {
  manifest = JSON.parse(read('manifest.json'));
  if (manifest.manifest_version !== 3) throw new Error('manifest_version is not 3');
});

// --- 2. every file referenced by the manifest exists -----------------------
check('every file referenced by the manifest exists', () => {
  if (!manifest) throw new Error('manifest did not parse');
  const referenced = [];
  const add = (value, why) => {
    if (typeof value === 'string' && value) referenced.push([value, why]);
  };

  add(manifest.background && manifest.background.service_worker, 'background.service_worker');
  add(manifest.action && manifest.action.default_popup, 'action.default_popup');
  add(manifest.options_page, 'options_page');
  add(manifest.options_ui && manifest.options_ui.page, 'options_ui.page');

  for (const entry of manifest.content_scripts || []) {
    for (const file of entry.js || []) add(file, 'content_scripts.js');
    for (const file of entry.css || []) add(file, 'content_scripts.css');
  }
  for (const [size, file] of Object.entries(manifest.icons || {})) add(file, `icons.${size}`);
  for (const [size, file] of Object.entries((manifest.action && manifest.action.default_icon) || {})) {
    add(file, `action.default_icon.${size}`);
  }
  for (const entry of manifest.web_accessible_resources || []) {
    for (const file of entry.resources || []) {
      // Glob patterns are allowed there; only check plain paths.
      if (!/[*?]/.test(file)) add(file, 'web_accessible_resources');
    }
  }

  if (!referenced.length) throw new Error('manifest references no files at all');
  const missing = referenced.filter(([file]) => !existsSync(join(root, file)));
  if (missing.length) throw new Error('missing: ' + missing.map(([f]) => f).join(', '));

  // Also verify the local files pulled in by the popup HTML.
  for (const file of ['popup.html']) assertFile(file, 'popup');
  const html = read('popup.html');
  const assets = [...html.matchAll(/(?:src|href)="([^"#:]+)"/g)].map((m) => m[1]);
  const missingAssets = assets.filter((asset) => !existsSync(join(root, asset)));
  if (missingAssets.length) throw new Error('popup.html references missing ' + missingAssets.join(', '));
  return true;
});

// --- 3. no innerHTML assignment in the content script ----------------------
check('content.js has no innerHTML assignment', () => {
  const source = read('content.js');
  const match = source.match(/\.innerHTML\s*=/);
  if (match) throw new Error('found ".innerHTML =" in content.js');
  if (/insertAdjacentHTML\s*\(/.test(source)) throw new Error('found insertAdjacentHTML in content.js');
  if (/\beval\s*\(/.test(source)) throw new Error('found eval( in content.js');
  return true;
});

// --- 4. the service worker uses alarms, not setInterval --------------------
check('background.js has no setInterval (keepalive uses chrome.alarms)', () => {
  const source = read('background.js');
  if (/setInterval\s*\(/.test(source)) throw new Error('found setInterval in background.js');
  if (!/chrome\.alarms\.create\s*\(/.test(source)) throw new Error('chrome.alarms.create is missing');
  if (!/chrome\.alarms\.onAlarm\.addListener/.test(source)) throw new Error('chrome.alarms.onAlarm handler is missing');
  return true;
});

// --- 5. the anchoring contract is present in content.js --------------------
check('content.js contains the anchoring contract', () => {
  const source = read('content.js');
  for (const needle of ['translateY(-100%)', 'position:absolute']) {
    if (!source.includes(needle)) throw new Error(`content.js does not mention ${needle}`);
  }
  // Percent based placement and the incremental card map have to be there too.
  if (!source.includes("'%'")) throw new Error('content.js does not build percentage coordinates');
  if (!/pointer-events:none/.test(source)) throw new Error('content.js does not disable pointer events on the overlay');
  return true;
});

// --- 6. protocol surface sanity (extension side of the wire protocol) ------
check('protocol messages are implemented on both sides', () => {
  const background = read('background.js');
  const content = read('content.js');
  if (!/ws:\/\/\$\{HOST\}:\$\{settings\.port\}\$\{WS_PATH\}/.test(background)) {
    throw new Error('the WebSocket URL is not built from the configured port');
  }
  if (!background.includes("'/ws'")) throw new Error('the /ws path is missing in background.js');
  for (const type of ['hello', 'register', 'request', 'cancel', 'status']) {
    if (!background.includes(`'${type}'`)) throw new Error(`background.js never sends "${type}"`);
  }
  for (const type of ['panel', 'progress', 'error', 'welcome', 'status']) {
    if (!background.includes(`case '${type}'`)) throw new Error(`background.js never routes "${type}"`);
  }
  if (!/chrome\.runtime\.connect\(\{\s*name:\s*'mt'\s*\}\)/.test(content)) {
    throw new Error('content.js does not connect with chrome.runtime.connect({name:"mt"})');
  }
  if (!/textContent/.test(content)) throw new Error('content.js never uses textContent');
  return true;
});

// --- 7. manifest details asked for by the specification --------------------
check('manifest matches the required shape', () => {
  if (!manifest) throw new Error('manifest did not parse');
  const eq = (actual, expected, what) => {
    if (actual !== expected) throw new Error(`${what}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  };
  eq(manifest.name, 'MangaTranslator Bridge', 'name');
  eq(manifest.version, '1.0.0', 'version');
  if (manifest.default_locale) throw new Error('default_locale must not be used');
  eq(manifest.background.service_worker, 'background.js', 'background.service_worker');
  eq(manifest.action.default_popup, 'popup.html', 'action.default_popup');
  eq(manifest.action.default_title, 'MangaTranslator', 'action.default_title');
  const cs = (manifest.content_scripts || [])[0];
  if (!cs) throw new Error('no content_scripts entry');
  eq(cs.run_at, 'document_idle', 'content_scripts.run_at');
  eq(cs.all_frames, false, 'content_scripts.all_frames');
  const perms = manifest.permissions || [];
  for (const permission of ['storage', 'tabs', 'activeTab', 'scripting', 'alarms']) {
    if (!perms.includes(permission)) throw new Error(`permission "${permission}" is missing`);
  }
  const hosts = manifest.host_permissions || [];
  for (const host of ['http://127.0.0.1/*', 'ws://127.0.0.1/*']) {
    if (!hosts.includes(host)) throw new Error(`host permission "${host}" is missing`);
  }
  const command = (manifest.commands || {})['toggle-translation'];
  if (!command) throw new Error('command "toggle-translation" is missing');
  eq(command.suggested_key && command.suggested_key.default, 'Ctrl+Shift+Y', 'command suggested key');
  if (!command.description || !/[А-Яа-яЁё]/.test(command.description)) {
    throw new Error('command description is not Russian');
  }
  if (manifest.icons) {
    for (const [size, file] of Object.entries(manifest.icons)) {
      if (!existsSync(join(root, file))) throw new Error(`icon ${size} (${file}) does not exist`);
    }
  }
  return true;
});

// --- 8. required files are present -----------------------------------------
check('all required extension files exist', () => {
  for (const file of [
    'manifest.json',
    'background.js',
    'content.js',
    'styles.css',
    'popup.html',
    'popup.js',
    'popup.css',
    'README.md',
    'tools/selfcheck.mjs',
  ]) {
    assertFile(file, 'required by the specification');
  }
  return true;
});

// --- report ----------------------------------------------------------------
const failed = results.filter((r) => !r.ok);
for (const result of results) {
  const mark = result.ok ? 'PASS' : 'FAIL';
  console.log(`${mark}  ${result.name}${result.ok ? '' : ' — ' + result.error}`);
}
console.log('');
console.log(`${results.length - failed.length}/${results.length} checks passed in ${relative(process.cwd(), root) || '.'}`);
process.exit(failed.length ? 1 : 0);
