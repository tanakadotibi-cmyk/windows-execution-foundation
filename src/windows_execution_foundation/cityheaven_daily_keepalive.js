'use strict';

// One-shot, read-only CityHeaven authentication keepalive. Deliberately emits
// only a fixed-origin classification record and never reads a response body.
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const EXPECTED_ORIGIN = 'https://newmanager.cityheaven.net';
const MANAGER_PATH = '/C2GirlList.php';
const LOGIN_PATH = '/C1Login.php';

function fail() { throw new Error('KEEPALIVE_FAILED'); }

function cli(argv = process.argv.slice(2)) {
  const value = name => {
    const index = argv.indexOf(name);
    return index >= 0 ? argv[index + 1] : undefined;
  };
  const port = Number(value('--cdp-port'));
  const output = value('--output');
  if (!Number.isInteger(port) || port < 1 || port > 65535 || !output) fail();
  return { port, output: path.resolve(output) };
}

function classifyResult(value) {
  const status = Number(value?.http_status);
  const origin = String(value?.origin || '');
  const pathname = String(value?.pathname || '');
  const redirected = value?.redirected === true;
  let authClass = 'UNKNOWN';
  if (origin === EXPECTED_ORIGIN && pathname === LOGIN_PATH) authClass = 'AUTH_REQUIRED';
  else if (status === 200 && origin === EXPECTED_ORIGIN && pathname === MANAGER_PATH) authClass = 'AUTHENTICATED';
  return {
    http_status: Number.isInteger(status) && status >= 100 && status <= 599 ? status : 0,
    final_origin: EXPECTED_ORIGIN,
    final_pathname: origin === EXPECTED_ORIGIN && pathname.startsWith('/') && !/[?#]/.test(pathname) ? pathname : '',
    redirected,
    auth_class: authClass,
  };
}

function getTargets(port) {
  return new Promise((resolve, reject) => {
    const request = http.get({ host: '127.0.0.1', port, path: '/json/list', timeout: 3000 }, response => {
      let value = '';
      response.setEncoding('utf8');
      response.on('data', chunk => {
        value += chunk;
        if (value.length > 2_000_000) request.destroy();
      });
      response.on('end', () => {
        try {
          const parsed = JSON.parse(value);
          resolve(Array.isArray(parsed) ? parsed : []);
        } catch { reject(new Error('KEEPALIVE_FAILED')); }
      });
    });
    request.on('timeout', () => request.destroy(new Error('KEEPALIVE_FAILED')));
    request.on('error', () => reject(new Error('KEEPALIVE_FAILED')));
  });
}

function managerTarget(targets) {
  return targets.find(item => {
    if (item?.type !== 'page' || typeof item.webSocketDebuggerUrl !== 'string') return false;
    try { return new URL(item.url).hostname === 'newmanager.cityheaven.net'; }
    catch { return false; }
  });
}

function evaluate(webSocketDebuggerUrl) {
  const expression = `(${async function keepalive() {
    const response = await fetch(
      'https://newmanager.cityheaven.net/C2GirlList.php?shopdir=s_apronlady',
      { method: 'GET', credentials: 'include', cache: 'no-store', redirect: 'follow' },
    );
    const final = new URL(response.url);
    return {
      http_status: response.status,
      origin: final.origin,
      pathname: final.pathname,
      redirected: response.redirected,
    };
  }.toString()})()`;
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(webSocketDebuggerUrl);
    let settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { socket.close(); } catch { /* already closed */ }
      if (error) reject(error); else resolve(value);
    };
    const timer = setTimeout(() => finish(new Error('KEEPALIVE_FAILED')), 15000);
    socket.onopen = () => socket.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: {
      expression, returnByValue: true, awaitPromise: true,
    } }));
    socket.onerror = () => finish(new Error('KEEPALIVE_FAILED'));
    socket.onclose = () => finish(new Error('KEEPALIVE_FAILED'));
    socket.onmessage = event => {
      let message;
      try { message = JSON.parse(event.data); } catch { finish(new Error('KEEPALIVE_FAILED')); return; }
      if (message.id !== 1) return;
      if (message.error || message.result?.exceptionDetails) finish(new Error('KEEPALIVE_FAILED'));
      else finish(null, message.result?.result?.value);
    };
  });
}

function timestampJst(now = new Date()) {
  const parts = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Tokyo', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).formatToParts(now).reduce((result, item) => ({ ...result, [item.type]: item.value }), {});
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}+09:00`;
}

function appendRecord(output, record) {
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.appendFileSync(output, JSON.stringify(record) + '\n', { encoding: 'utf8', flag: 'a' });
}

async function main() {
  const options = cli();
  let result = { http_status: 0, final_origin: EXPECTED_ORIGIN, final_pathname: '', redirected: false, auth_class: 'UNKNOWN' };
  try {
    const target = managerTarget(await getTargets(options.port));
    if (target) result = classifyResult(await evaluate(target.webSocketDebuggerUrl));
  } catch { /* fail closed using the sanitized UNKNOWN record */ }
  const record = { timestamp_jst: timestampJst(), ...result };
  appendRecord(options.output, record);
  process.stdout.write(JSON.stringify(record));
  process.exitCode = record.auth_class === 'AUTHENTICATED' ? 0 : 20;
}

if (require.main === module) main();

module.exports = { EXPECTED_ORIGIN, LOGIN_PATH, MANAGER_PATH, classifyResult, managerTarget, timestampJst };
