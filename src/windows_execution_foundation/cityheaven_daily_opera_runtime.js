'use strict';

// Dedicated, headful Opera lifecycle for CityHeaven Daily. This module never
// enumerates, closes, restarts, or changes any unrelated browser process.
const childProcess = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const { setTimeout: delay } = require('node:timers/promises');

function fault(code) { return Object.assign(new Error(code), { runtimeCode: code }); }
function normalizePath(value) {
  return path.resolve(String(value || '')).replace(/[\\/]+$/, '').toLowerCase();
}

function runtimeConfig(value) {
  if (!value || value.browser !== 'OPERA') throw fault('OPERA_RUNTIME_CONFIG_INVALID');
  if (!Number.isInteger(value.cdp_port) || value.cdp_port < 9236 || value.cdp_port > 9245) {
    throw fault('OPERA_RUNTIME_CONFIG_INVALID');
  }
  for (const key of ['opera_executable', 'dedicated_profile_path', 'startup_lock_path']) {
    if (typeof value[key] !== 'string' || !value[key].trim()) throw fault('OPERA_RUNTIME_CONFIG_INVALID');
  }
  const profile = path.resolve(value.dedicated_profile_path);
  if (path.basename(profile).toLowerCase() !== 'dailymetricsoperaprofile') {
    throw fault('OPERA_RUNTIME_CONFIG_INVALID');
  }
  return Object.freeze({
    browser: 'OPERA', cdpPort: value.cdp_port,
    executable: path.resolve(value.opera_executable), profile,
    lockPath: path.resolve(value.startup_lock_path),
    startUrl: value.start_url || 'https://newmanager.cityheaven.net/',
  });
}

async function fetchJson(url, timeout = 3000) {
  const response = await fetch(url, { signal: AbortSignal.timeout(timeout) });
  if (!response.ok) throw fault('CDP_RESPONSE_INVALID');
  return response.json();
}

function tcpListening(port) {
  return new Promise(resolve => {
    const socket = net.createConnection({ host: '127.0.0.1', port });
    const finish = value => { socket.destroy(); resolve(value); };
    socket.setTimeout(1000);
    socket.once('connect', () => finish(true));
    socket.once('timeout', () => finish(false));
    socket.once('error', () => finish(false));
  });
}

function command(target, method, params = {}) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(target);
    let settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true; clearTimeout(timer);
      try { socket.close(); } catch { /* already closed */ }
      if (error) reject(error); else resolve(value);
    };
    const timer = setTimeout(() => finish(fault('CDP_TIMEOUT')), 5000);
    socket.onopen = () => socket.send(JSON.stringify({ id: 1, method, params }));
    socket.onerror = () => finish(fault('CDP_CONNECTION_FAILED'));
    socket.onclose = () => finish(fault('CDP_DISCONNECTED'));
    socket.onmessage = event => {
      try {
        const message = JSON.parse(event.data);
        if (message.id !== 1) return;
        if (message.error) finish(fault('CDP_COMMAND_FAILED'));
        else finish(null, message.result);
      } catch { finish(fault('CDP_RESPONSE_INVALID')); }
    };
  });
}

async function inspectPort(config, dependencies = {}) {
  const getJson = dependencies.fetchJson || fetchJson;
  let version;
  try { version = await getJson(`http://127.0.0.1:${config.cdpPort}/json/version`); }
  catch {
    const listening = await (dependencies.tcpListening || tcpListening)(config.cdpPort);
    return { state: listening ? 'MISMATCH' : 'ABSENT' };
  }
  if (!version || typeof version.webSocketDebuggerUrl !== 'string'
    || !/\bOPR\/\d/i.test(String(version['User-Agent'] || ''))) return { state: 'MISMATCH' };
  let arguments_;
  try {
    arguments_ = (await (dependencies.command || command)(
      version.webSocketDebuggerUrl, 'Browser.getBrowserCommandLine'
    ))?.arguments;
  } catch { return { state: 'STARTING' }; }
  if (!Array.isArray(arguments_)) return { state: 'MISMATCH' };
  const port = arguments_.find(argument => argument.startsWith('--remote-debugging-port='));
  const profile = arguments_.find(argument => argument.startsWith('--user-data-dir='));
  const matches = port === `--remote-debugging-port=${config.cdpPort}`
    && profile && normalizePath(profile.slice('--user-data-dir='.length)) === normalizePath(config.profile);
  return { state: matches ? 'MATCH' : 'MISMATCH' };
}

function launchOpera(config, dependencies = {}) {
  const exists = dependencies.exists || fs.existsSync;
  if (!exists(config.executable)) throw fault('OPERA_RUNTIME_UNAVAILABLE');
  const profileExisted = exists(config.profile);
  const spawn = dependencies.spawn || childProcess.spawn;
  return new Promise((resolve, reject) => {
    const child = spawn(config.executable, [
      `--remote-debugging-port=${config.cdpPort}`, `--user-data-dir=${config.profile}`,
      '--enable-automation', '--no-first-run', '--no-default-browser-check', config.startUrl,
    ], { detached: true, stdio: 'ignore', windowsHide: false, shell: false });
    child.once('error', () => reject(fault('BROWSER_LAUNCH_FAILED')));
    child.once('spawn', () => { child.unref?.(); resolve({ profileCreated: !profileExisted }); });
  });
}

function createRuntimeEnsurer(rawConfig, dependencies = {}) {
  const config = runtimeConfig(rawConfig);
  let inFlight = null;
  async function ensure() {
    const inspect = dependencies.inspect || (value => inspectPort(value, dependencies));
    const initial = await inspect(config);
    if (initial.state === 'MATCH') return { reused: true, profileCreated: false };
    if (initial.state !== 'ABSENT') throw fault('CDP_PORT_RUNTIME_MISMATCH');
    fs.mkdirSync(path.dirname(config.lockPath), { recursive: true });
    let lock;
    try { lock = fs.openSync(config.lockPath, 'wx'); }
    catch { throw fault('OPERA_START_LOCKED'); }
    try {
      const launched = await (dependencies.launch || launchOpera)(config, dependencies);
      // At most two bounded post-start checks; there is deliberately no blind retry loop.
      await (dependencies.wait || delay)(2500);
      let current = await inspect(config);
      if (current.state === 'MISMATCH') throw fault('CDP_PORT_RUNTIME_MISMATCH');
      if (current.state === 'ABSENT' || current.state === 'STARTING') {
        await (dependencies.wait || delay)(2500);
        current = await inspect(config);
        if (current.state === 'MISMATCH') throw fault('CDP_PORT_RUNTIME_MISMATCH');
      }
      if (current.state !== 'MATCH') throw fault('BROWSER_CDP_UNAVAILABLE');
      return { reused: false, profileCreated: launched.profileCreated };
    } finally {
      try { if (lock !== undefined) fs.closeSync(lock); } catch { /* no-op */ }
      try { fs.unlinkSync(config.lockPath); } catch { /* no-op */ }
    }
  }
  return { config, ensure() { if (!inFlight) inFlight = ensure().finally(() => { inFlight = null; }); return inFlight; } };
}

async function readiness(rawConfig, dependencies = {}) {
  const config = runtimeConfig(rawConfig);
  const lifecycle = await createRuntimeEnsurer(rawConfig, dependencies).ensure();
  const getJson = dependencies.fetchJson || fetchJson;
  const targets = await getJson(`http://127.0.0.1:${config.cdpPort}/json`, 5000);
  if (!Array.isArray(targets)) throw fault('CDP_DISCOVERY_FAILED');
  const pages = targets.filter(target => target.type === 'page' && typeof target.webSocketDebuggerUrl === 'string');
  const city = pages.find(target => {
    try { return /(^|\.)cityheaven\.net$/i.test(new URL(target.url).hostname); } catch { return false; }
  });
  const target = city || pages[0];
  let session = { cityHost: false, authGate: false, managerReady: false };
  if (target) {
    const result = await (dependencies.command || command)(target.webSocketDebuggerUrl, 'Runtime.evaluate', {
      expression: `(${function inspectCurrentPage() {
        const text = location.pathname + location.search + ' ' + document.title;
        const cityHost = /(^|\.)cityheaven\.net$/i.test(location.hostname);
        const authGate = /login|signin|auth|mfa|captcha|ログイン|認証/i.test(text)
          || Boolean(document.querySelector('input[type="password"],iframe[src*="captcha" i],[class*="captcha" i]'));
        return { cityHost, authGate, managerReady: cityHost && !authGate && document.readyState !== 'loading' };
      }.toString()})()`, returnByValue: true, awaitPromise: false,
    });
    if (result?.exceptionDetails) throw fault('CDP_EVALUATION_FAILED');
    session = result?.result?.value || session;
  }
  const authState = session.managerReady ? 'AUTHENTICATED'
    : (session.authGate || city) ? 'AUTH_REQUIRED' : 'UNDETERMINED';
  return {
    cdp_reachable: true, auth_state: authState,
    cityheaven_manager_ready: Boolean(session.managerReady),
    existing_profile_reused: lifecycle.reused,
    new_profile_created: lifecycle.profileCreated,
  };
}

function loadConfig(configPath) {
  const parsed = JSON.parse(fs.readFileSync(path.resolve(configPath), 'utf8'));
  return parsed.cityheaven_daily;
}

async function main() {
  const index = process.argv.indexOf('--config');
  if (index < 0 || !process.argv[index + 1]) throw fault('OPERA_RUNTIME_CONFIG_INVALID');
  process.stdout.write(JSON.stringify(await readiness(loadConfig(process.argv[index + 1]))));
}

if (require.main === module) main().catch(error => {
  process.stderr.write(String(error?.runtimeCode || error?.message || 'OPERA_RUNTIME_UNAVAILABLE') + '\n');
  process.exitCode = 20;
});

module.exports = { createRuntimeEnsurer, inspectPort, launchOpera, loadConfig, readiness, runtimeConfig };
