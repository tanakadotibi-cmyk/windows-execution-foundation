'use strict';

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
  if (!value || value.browser !== 'CHROME') throw fault('CHROME_RUNTIME_CONFIG_INVALID');
  if (!Number.isInteger(value.cdp_port) || value.cdp_port < 9227 || value.cdp_port > 9235) {
    throw fault('CHROME_RUNTIME_CONFIG_INVALID');
  }
  for (const key of ['chrome_executable', 'profile_path', 'base_reader_path', 'startup_lock_path']) {
    if (typeof value[key] !== 'string' || !value[key].trim()) throw fault('CHROME_RUNTIME_CONFIG_INVALID');
  }
  return Object.freeze({
    browser: 'CHROME', cdpPort: value.cdp_port,
    executable: path.resolve(value.chrome_executable), profile: path.resolve(value.profile_path),
    baseReaderPath: path.resolve(value.base_reader_path), lockPath: path.resolve(value.startup_lock_path),
    startUrl: value.start_url || 'https://newmanager.cityheaven.net/',
    restoreSession: value.restore_session === true,
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

function command(target, method, params = {}, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(fault('STOPPED')); return; }
    const socket = new WebSocket(target);
    let settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      try { socket.close(); } catch { /* already closed */ }
      if (error) reject(error); else resolve(value);
    };
    const abort = () => finish(fault('STOPPED'));
    const timer = setTimeout(() => finish(fault('CDP_TIMEOUT')), 5000);
    signal?.addEventListener('abort', abort, { once: true });
    socket.onopen = () => socket.send(JSON.stringify({ id: 1, method, params }));
    socket.onerror = () => finish(fault('CDP_CONNECTION_FAILED'));
    socket.onclose = () => finish(fault('CDP_DISCONNECTED'));
    socket.onmessage = event => {
      try {
        const message = JSON.parse(event.data);
        if (message.id !== 1) return;
        if (message.error) {
          const error = fault('CDP_COMMAND_FAILED');
          error.cdpCode = message.error.code;
          error.cdpMessage = message.error.message;
          finish(error);
        }
        else finish(null, message.result);
      } catch { finish(fault('CDP_RESPONSE_INVALID')); }
    };
  });
}

function isNoAutomationCommandLineError(error) {
  return error?.cdpCode === -32000
    && String(error?.cdpMessage || '').trim()
      === 'Command line not returned because --enable-automation not set.';
}

function bodyContainsExactProfile(body, profile) {
  const text = String(body || '').replace(/\//g, '\\').toLowerCase();
  const expected = normalizePath(profile).replace(/\//g, '\\');
  let offset = text.indexOf(expected);
  while (offset !== -1) {
    const before = offset === 0 ? '' : text[offset - 1];
    const after = text[offset + expected.length] || '';
    if ((!before || /[\s"]/.test(before)) && (!after || /[\s\\"]/.test(after))) return true;
    offset = text.indexOf(expected, offset + 1);
  }
  return false;
}

async function inspectProfileFromVersionPage(config, version, dependencies) {
  const send = dependencies.command || command;
  const getJson = dependencies.fetchJson || fetchJson;
  let targetId;
  let matched = false;
  let failed = false;
  try {
    const created = await send(version.webSocketDebuggerUrl, 'Target.createTarget', { url: 'chrome://version/' });
    targetId = created?.targetId;
    if (typeof targetId !== 'string' || !targetId) throw fault('CDP_RESPONSE_INVALID');
    const targets = await getJson(`http://127.0.0.1:${config.cdpPort}/json`);
    const target = Array.isArray(targets) ? targets.find(item => item?.id === targetId) : null;
    if (!target || typeof target.webSocketDebuggerUrl !== 'string') throw fault('CDP_DISCOVERY_FAILED');
    const evaluated = await send(target.webSocketDebuggerUrl, 'Runtime.evaluate', {
      expression: `(async () => {
        if (document.readyState === 'loading') {
          await new Promise(resolve => document.addEventListener('DOMContentLoaded', resolve, { once: true }));
        }
        return document.body.innerText;
      })()`,
      returnByValue: true, awaitPromise: true,
    });
    if (evaluated?.exceptionDetails || typeof evaluated?.result?.value !== 'string') {
      throw fault('CDP_EVALUATION_FAILED');
    }
    matched = bodyContainsExactProfile(evaluated.result.value, config.profile);
  } catch { failed = true; }
  finally {
    if (targetId) {
      try {
        const closed = await send(version.webSocketDebuggerUrl, 'Target.closeTarget', { targetId });
        if (closed?.success !== true) failed = true;
      } catch { failed = true; }
    }
  }
  return !failed && matched;
}

async function inspectPort(config, dependencies = {}) {
  const getJson = dependencies.fetchJson || fetchJson;
  let version;
  try { version = await getJson(`http://127.0.0.1:${config.cdpPort}/json/version`); }
  catch (error) {
    if (error?.runtimeCode === 'CDP_RESPONSE_INVALID') return { state: 'MISMATCH' };
    const listening = await (dependencies.tcpListening || tcpListening)(config.cdpPort);
    return { state: listening ? 'MISMATCH' : 'ABSENT' };
  }
  if (!version || typeof version.webSocketDebuggerUrl !== 'string'
    || !String(version.Browser || '').startsWith('Chrome/')) return { state: 'MISMATCH' };
  let arguments_;
  try {
    const result = await (dependencies.command || command)(version.webSocketDebuggerUrl, 'Browser.getBrowserCommandLine');
    arguments_ = result?.arguments;
  } catch (error) {
    if (!isNoAutomationCommandLineError(error)) return { state: 'MISMATCH' };
    const matches = await inspectProfileFromVersionPage(config, version, dependencies);
    return { state: matches ? 'MATCH' : 'MISMATCH' };
  }
  if (!Array.isArray(arguments_)) return { state: 'MISMATCH' };
  const port = arguments_.find(argument => argument.startsWith('--remote-debugging-port='));
  const profile = arguments_.find(argument => argument.startsWith('--user-data-dir='));
  const matches = port === `--remote-debugging-port=${config.cdpPort}`
    && profile && normalizePath(profile.slice('--user-data-dir='.length)) === normalizePath(config.profile);
  return { state: matches ? 'MATCH' : 'MISMATCH' };
}

function launchChrome(config, dependencies = {}) {
  const exists = dependencies.exists || fs.existsSync;
  if (!exists(config.executable)) throw fault('CHROME_RUNTIME_UNAVAILABLE');
  const profileExisted = exists(config.profile);
  if (!profileExisted) (dependencies.mkdir || fs.mkdirSync)(config.profile, { recursive: true });
  const spawn = dependencies.spawn || childProcess.spawn;
  return new Promise((resolve, reject) => {
    const child = spawn(config.executable, [
      `--remote-debugging-port=${config.cdpPort}`, `--user-data-dir=${config.profile}`,
      '--profile-directory=Default', '--no-first-run', '--no-default-browser-check',
      ...(config.restoreSession ? ['--restore-last-session'] : [config.startUrl]),
    ], { detached: true, stdio: 'ignore', windowsHide: true, shell: false });
    child.once('error', () => reject(fault('BROWSER_LAUNCH_FAILED')));
    child.once('spawn', () => { child.unref?.(); resolve({ profileCreated: !profileExisted }); });
  });
}

function createRuntimeEnsurer(rawConfig, dependencies = {}) {
  const config = runtimeConfig(rawConfig);
  let inFlight = null;
  let launchLatched = false;
  async function ensure(signal) {
    const inspect = dependencies.inspect || (value => inspectPort(value, dependencies));
    const initial = await inspect(config);
    if (initial.state === 'MATCH') return { reused: true, profileCreated: false };
    if (initial.state !== 'ABSENT') throw fault('CDP_PORT_RUNTIME_MISMATCH');
    if (launchLatched) throw fault('CHROME_START_ALREADY_ATTEMPTED');
    launchLatched = true;
    fs.mkdirSync(path.dirname(config.lockPath), { recursive: true });
    let lock;
    try { lock = fs.openSync(config.lockPath, 'wx'); }
    catch (error) {
      if (error.code !== 'EEXIST') throw fault('CHROME_START_LOCK_FAILED');
      for (let attempt = 0; attempt < 16; attempt++) {
        if (signal?.aborted) throw fault('STOPPED');
        await (dependencies.wait || delay)(500);
        const concurrent = await inspect(config);
        if (concurrent.state === 'MATCH') return { reused: true, profileCreated: false };
        if (concurrent.state === 'MISMATCH') throw fault('CDP_PORT_RUNTIME_MISMATCH');
      }
      throw fault('CHROME_START_IN_PROGRESS');
    }
    try {
      const launched = await (dependencies.launch || launchChrome)(config, dependencies);
      for (let attempt = 0; attempt < 16; attempt++) {
        if (signal?.aborted) throw fault('STOPPED');
        await (dependencies.wait || delay)(500);
        const current = await inspect(config);
        if (current.state === 'MATCH') return { reused: false, profileCreated: launched.profileCreated };
        if (current.state === 'MISMATCH') throw fault('CDP_PORT_RUNTIME_MISMATCH');
      }
      throw fault('BROWSER_CDP_UNAVAILABLE');
    } finally {
      try { if (lock !== undefined) fs.closeSync(lock); } catch { /* no-op */ }
      try { fs.unlinkSync(config.lockPath); } catch { /* no-op */ }
    }
  }
  return {
    config,
    ensure(signal) {
      if (!inFlight) inFlight = ensure(signal).finally(() => { inFlight = null; });
      return inFlight;
    },
  };
}

async function listTargets(port, signal) {
  let targets;
  try { targets = await fetchJson(`http://127.0.0.1:${port}/json`, 5000); }
  catch { throw fault('CDP_DISCOVERY_FAILED'); }
  if (!Array.isArray(targets)) throw fault('CDP_DISCOVERY_FAILED');
  return targets.filter(target => target.type === 'page' && typeof target.webSocketDebuggerUrl === 'string');
}

function chooseCityTarget(targets, preferredId) {
  const pages = targets.filter(target => {
    try { return new URL(target.url).hostname === 'pro-manager.cityheaven.net'; } catch { return false; }
  });
  return pages.find(target => target.id === preferredId)
    || pages.find(target => new URL(target.url).pathname === '/reservation/list')
    || pages.find(target => !/chat|login/i.test(new URL(target.url).pathname)) || pages[0] || null;
}

async function proManagerStateInPage() {
  if (location.origin !== 'https://pro-manager.cityheaven.net') {
    return { authenticated: false, ready: false, authRequired: false, transferFailed: true };
  }
  const gate = /login|signin|auth|mfa|captcha/i.test(location.pathname + location.search)
    || Boolean(document.querySelector('input[type="password"],iframe[src*="captcha" i],[class*="captcha" i]'));
  if (gate) return { authenticated: false, ready: false, authRequired: true, transferFailed: false };
  const text = `${document.title || ''}\n${document.body?.innerText || ''}`;
  if (/\b401\b|unauthorized/i.test(text)) {
    return { authenticated: false, ready: false, authRequired: false, transferFailed: true };
  }
  const supportedPath = location.pathname === '/'
    || location.pathname === '/reservation/timechart'
    || location.pathname === '/reservation/list';
  const shellReady = supportedPath && Boolean(String(document.title || '').trim())
    && (/ホーム|受付台帳/i.test(document.title || '')
      || Boolean(document.querySelector('a[href*="/reservation/"],a[href*="logout" i],.day-td-rsvlist')));
  if (!shellReady) {
    return { authenticated: false, ready: false, authRequired: false, transferFailed: false };
  }
  try {
    const response = await fetch('/reservation/timechart', {
      method: 'GET', credentials: 'include', cache: 'no-store', redirect: 'follow',
      headers: { Accept: 'text/html' },
    });
    const finalUrl = new URL(response.url || '/reservation/timechart', location.origin);
    const ledger = new DOMParser().parseFromString(await response.text(), 'text/html');
    const ledgerGate = /login|signin|auth|mfa|captcha/i.test(finalUrl.pathname + finalUrl.search)
      || Boolean(ledger.querySelector('input[type="password"],iframe[src*="captcha" i],[class*="captcha" i]'));
    const unauthorized = response.status === 401 || response.status === 403
      || /\b401\b|unauthorized/i.test(`${ledger.title || ''}\n${ledger.body?.innerText || ''}`);
    const ready = response.ok && !ledgerGate && !unauthorized
      && finalUrl.origin === location.origin && finalUrl.pathname === '/reservation/timechart';
    return { authenticated: ready, ready, authRequired: ledgerGate,
      transferFailed: unauthorized, ledgerHttpStatus: response.status };
  } catch {
    return { authenticated: false, ready: false, authRequired: false, transferFailed: false };
  }
}

async function cmsReservationLoginStateInPage(shopdir, openLink) {
  const gate = /login|signin|auth|mfa|captcha/i.test(location.pathname + location.search)
    || Boolean(document.querySelector('input[type="password"],iframe[src*="captcha" i],[class*="captcha" i]'));
  // A cached C1Main tab can outlive its login. Check the current session before opening ReservationLogin.
  let liveSession = false;
  let liveAuthRequired = gate;
  if (!gate) {
    try {
      const response = await fetch('/C1Main.php', {
        method: 'GET', credentials: 'include', cache: 'no-store', redirect: 'follow',
        headers: { Accept: 'text/html' },
      });
      const finalUrl = new URL(response.url || '/C1Main.php', location.origin);
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      liveAuthRequired = /login|signin|auth|mfa|captcha/i.test(finalUrl.pathname + finalUrl.search)
        || Boolean(page.querySelector('input[type="password"],iframe[src*="captcha" i]'))
        || response.status === 401;
      liveSession = response.ok && !liveAuthRequired && finalUrl.origin === location.origin
        && /^\/C1Main\.php$/i.test(finalUrl.pathname);
    } catch { /* fail closed if the CMS cannot confirm its session */ }
  }
  const expectedHref = `https://newmanager.cityheaven.net/ReservationLogin?shopdir=${encodeURIComponent(shopdir)}`;
  const links = [...document.querySelectorAll('a[href]')].filter(anchor => {
    try {
      const url = new URL(anchor.getAttribute('href'), location.href);
      return url.href === expectedHref
        && String(anchor.getAttribute('target') || '').toLowerCase() === 'pro-manager';
    } catch { return false; }
  });
  // The CMS can render the same exact official link in several navigation areas.
  // Each match has already passed the exact href and target check above.
  const hasOfficialLink = links.length > 0;
  const authenticated = liveSession && /^\/C1Main\.php$/i.test(location.pathname) && hasOfficialLink;
  let opened = false;
  if (openLink && authenticated) opened = Boolean(window.open(links[0].href, 'pro-manager'));
  return { authenticated, authRequired: liveAuthRequired, linkCount: hasOfficialLink ? 1 : 0,
    officialHref: hasOfficialLink ? expectedHref : null, opened };
}

async function evaluate(target, fn, args, signal) {
  const expression = `(${fn.toString()})(${args.map(value => JSON.stringify(value)).join(',')})`;
  const result = await command(target.webSocketDebuggerUrl, 'Runtime.evaluate', {
    expression, returnByValue: true, awaitPromise: true,
  }, signal);
  if (result?.exceptionDetails) throw fault('CDP_EVALUATION_FAILED');
  return result?.result?.value;
}

async function createPageTarget(config, url, dependencies = {}) {
  const getJson = dependencies.fetchJson || fetchJson;
  const send = dependencies.command || command;
  let version;
  try { version = await getJson(`http://127.0.0.1:${config.cdpPort}/json/version`, 5000); }
  catch { throw fault('CDP_DISCOVERY_FAILED'); }
  if (typeof version?.webSocketDebuggerUrl !== 'string') throw fault('CDP_DISCOVERY_FAILED');
  const created = await send(version.webSocketDebuggerUrl, 'Target.createTarget', { url });
  if (typeof created?.targetId !== 'string' || !created.targetId) throw fault('CITY_AUTH_TRANSFER_FAILED');
  return created.targetId;
}

async function bootstrapOfficialAuth(config, signal, dependencies = {}) {
  const getTargets = dependencies.listTargets || (value => listTargets(value.cdpPort, signal));
  const inspectPro = dependencies.inspectProTarget
    || (target => evaluate(target, proManagerStateInPage, [], signal));
  const inspectCms = dependencies.inspectCmsTarget
    || (target => evaluate(target, cmsReservationLoginStateInPage, ['s_apronlady', false], signal));
  const openCms = dependencies.openCmsLink || (target => {
    const expression = `(${cmsReservationLoginStateInPage.toString()})('s_apronlady',true)`;
    return (dependencies.command || command)(target.webSocketDebuggerUrl, 'Runtime.evaluate', {
      expression, returnByValue: true, awaitPromise: true, userGesture: true,
    }, signal).then(result => {
      if (result?.exceptionDetails) throw fault('CDP_EVALUATION_FAILED');
      return result?.result?.value;
    });
  });
  const wait = dependencies.wait || delay;
  const createTarget = dependencies.createTarget || (url => createPageTarget(config, url, dependencies));
  const byHost = (targets, host) => targets.filter(target => {
    try { return new URL(target.url).hostname === host; } catch { return false; }
  });
  const classifyPro = async (targets, requireUnique) => {
    const states = await Promise.all(byHost(targets, 'pro-manager.cityheaven.net').map(async target => (
      { target, state: await inspectPro(target) }
    )));
    const authenticated = states.filter(item => item.state?.authenticated === true);
    if (requireUnique && authenticated.length > 1) throw fault('CITY_AUTH_TRANSFER_FAILED');
    if (authenticated.length >= 1) {
      const target = chooseCityTarget(authenticated.map(item => item.target));
      return { target, state: authenticated.find(item => item.target.id === target.id).state };
    }
    // A stale existing pro-manager tab must not block the official CMS recovery route.
    // After a handoff, retain the fail-closed transfer check.
    if (requireUnique && states.some(item => item.state?.transferFailed === true)) throw fault('CITY_AUTH_TRANSFER_FAILED');
    return null;
  };

  let targets = await getTargets(config);
  let pro = await classifyPro(targets, false);
  if (pro) return { target: pro.target, proManagerState: pro.state,
    existingProManagerReused: true, cmsEntryUsed: false, officialHandoffPass: true };

  let cmsTargets = byHost(targets, 'newmanager.cityheaven.net');
  if (cmsTargets.length === 0) {
    const targetId = await createTarget('https://newmanager.cityheaven.net/');
    for (let attempt = 0; attempt < 16 && cmsTargets.length === 0; attempt++) {
      if (signal?.aborted) throw fault('STOPPED');
      await wait(500);
      targets = await getTargets(config);
      cmsTargets = byHost(targets, 'newmanager.cityheaven.net')
        .filter(target => target.id === targetId || cmsTargets.length === 0);
    }
  }
  if (cmsTargets.length === 0) throw fault('CITY_AUTH_TRANSFER_FAILED');
  const cmsStates = await Promise.all(cmsTargets.map(async target => (
    { target, state: await inspectCms(target) }
  )));
  const eligible = cmsStates.filter(item => item.state?.authenticated === true);
  if (eligible.length === 0 && cmsStates.some(item => item.state?.authRequired === true)) {
    throw fault('AUTH_REQUIRED_CMS');
  }
  if (eligible.length !== 1 || eligible[0].state?.linkCount !== 1) throw fault('CITY_AUTH_TRANSFER_FAILED');
  const opened = await openCms(eligible[0].target);
  if (opened?.opened !== true || opened?.linkCount !== 1) throw fault('CITY_AUTH_TRANSFER_FAILED');

  for (let attempt = 0; attempt < 16; attempt++) {
    if (signal?.aborted) throw fault('STOPPED');
    await wait(500);
    targets = await getTargets(config);
    pro = await classifyPro(targets, true);
    if (pro) return { target: pro.target, proManagerState: pro.state,
      existingProManagerReused: false, cmsEntryUsed: true, officialHandoffPass: true };
  }
  throw fault('CITY_AUTH_TRANSFER_FAILED');
}

class BrowserReader {
  constructor(signal, rawConfig) {
    const config = runtimeConfig(rawConfig);
    const base = require(config.baseReaderPath);
    const ensurer = createRuntimeEnsurer(rawConfig);
    let authInFlight = null;
    class DedicatedReader extends base.BrowserReader {
      constructor() { super(signal); this.runtimeConfig = config; this.runtimeEnsurer = ensurer; }
      async ensure(kind) {
        if (kind !== 'city') throw fault('BROWSER_KIND_INVALID');
        const lifecycle = await this.runtimeEnsurer.ensure(this.signal);
        if (!authInFlight) authInFlight = bootstrapOfficialAuth(config, this.signal)
          .finally(() => { authInFlight = null; });
        const auth = await authInFlight;
        this.targetIds.city = auth.target.id;
        return { ...lifecycle, ...auth };
      }
      async target(kind) {
        if (kind !== 'city') throw fault('BROWSER_KIND_INVALID');
        const target = chooseCityTarget(await listTargets(config.cdpPort, this.signal), this.targetIds.city);
        if (!target) throw fault('CITY_TARGET_UNAVAILABLE');
        this.targetIds.city = target.id;
        return target;
      }
      async read(kind, fn, ...args) {
        return evaluate(await this.target(kind), fn, args, this.signal);
      }
    }
    return new DedicatedReader();
  }
}

async function readiness(rawConfig) {
  const controller = new AbortController();
  const config = runtimeConfig(rawConfig);
  const ensurer = createRuntimeEnsurer(rawConfig);
  const lifecycle = await ensurer.ensure(controller.signal);
  const auth = await bootstrapOfficialAuth(config, controller.signal);
  const state = auth.proManagerState
    || await evaluate(auth.target, proManagerStateInPage, [], controller.signal);
  return { ...classifyReadiness(state, state.authRequired === true, lifecycle),
    route: 'CMS_OFFICIAL_RESERVATION_HANDOFF',
    existing_pro_manager_reused: auth.existingProManagerReused,
    cms_entry_used: auth.cmsEntryUsed,
    official_handoff_pass: auth.officialHandoffPass === true };
}

async function dailyManagerReadiness(config, dependencies = {}) {
  const list = dependencies.listTargets || listTargets;
  const send = dependencies.command || command;
  const create = dependencies.createTarget || createPageTarget;
  let created = false;
  for (let attempt = 0; attempt < 16; attempt++) {
    const targets = await list(config.cdpPort);
    const target = targets.find(item => {
      try { return new URL(item.url).origin === 'https://newmanager.cityheaven.net'; }
      catch { return false; }
    });
    if (!target && !created) {
      await create(config, config.startUrl, dependencies);
      created = true;
    } else if (target) {
      const result = await send(target.webSocketDebuggerUrl, 'Runtime.evaluate', {
        expression: '({origin:location.origin,ready:document.readyState})', returnByValue: true,
      });
      const value = result?.result?.value;
      if (!result?.exceptionDetails && value?.origin === 'https://newmanager.cityheaven.net'
          && value?.ready === 'complete') return { ready: true, created };
    }
    await (dependencies.wait || delay)(500);
  }
  throw fault('BROWSER_CDP_UNAVAILABLE');
}

async function dailyReadiness(rawConfig, dependencies = {}) {
  const controller = new AbortController();
  const config = runtimeConfig(rawConfig);
  const exists = dependencies.exists || fs.existsSync;
  // Daily may start the established reservation runtime, but it must never create a profile.
  if (!exists(config.profile)) throw fault('CHROME_RUNTIME_UNAVAILABLE');
  const ensurer = createRuntimeEnsurer(rawConfig, dependencies);
  const lifecycle = await ensurer.ensure(controller.signal);
  const manager = await dailyManagerReadiness(config, dependencies);
  return {
    cdp_reachable: true,
    runtime_identity: 'MATCH',
    profile_identity: 'MATCH',
    cdp_port: config.cdpPort,
    auth_state: 'DEFERRED_TO_DAILY_READER',
    daily_manager_ready: manager.ready,
    existing_process_reused: lifecycle.reused,
    new_profile_created: lifecycle.profileCreated,
    additional_browser_launch_count: lifecycle.reused ? 0 : 1,
    browser_restart_count: 0,
    cms_entry_used: false,
    official_handoff_used: false,
  };
}

function classifyReadiness(state, authRequired, lifecycle) {
  return {
    cdp_reachable: true, auth_state: state.authenticated ? 'AUTHENTICATED' : authRequired ? 'AUTH_REQUIRED' : 'UNDETERMINED',
    reservation_list_ready: Boolean(state.authenticated && state.ready),
    existing_process_reused: lifecycle.reused, new_profile_created: lifecycle.profileCreated,
  };
}

async function dailySavedLogin(rawConfig) {
  return require('./daily_saved_login_runtime').recoverCity(runtimeConfig(rawConfig), {
    inspectPort, createPageTarget, listTargets, command, fetchJson,
  });
}

module.exports = { BrowserReader, createRuntimeEnsurer, inspectPort, launchChrome, runtimeConfig,
  classifyReadiness, readiness, dailyReadiness, dailyManagerReadiness, dailySavedLogin, bodyContainsExactProfile, isNoAutomationCommandLineError,
  bootstrapOfficialAuth, proManagerStateInPage, cmsReservationLoginStateInPage };
