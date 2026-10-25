'use strict';

// Read-only CDP adapter. It only evaluates same-origin GET/fetch operations and
// never exposes cookies, credentials, response bodies, or customer data.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
let diagnosticWritten = false;

function configuredPort(argv = process.argv.slice(2), environment = process.env) {
  const portIndex = argv.indexOf('--cdp-port');
  const configIndex = argv.indexOf('--config');
  let raw;
  if (portIndex >= 0) raw = argv[portIndex + 1];
  else if (configIndex >= 0 && argv[configIndex + 1]) {
    const config = JSON.parse(fs.readFileSync(path.resolve(argv[configIndex + 1]), 'utf8'));
    raw = config?.cityheaven_daily?.cdp_port;
  } else raw = environment.CITYHEAVEN_DAILY_CDP_PORT;
  // Keep the accepted general/manual route unchanged when no Daily override is supplied.
  if (raw === undefined || raw === '') return 9225;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 1 || value > 65535) fail('BROWSER_UNAVAILABLE');
  return value;
}
const PORT = configuredPort();
const SHOPDIR = 's_apronlady';
const COUNT_DISCOVERY_TIMEOUT_MS = 30000;
const TIMEOUT_BASE_MS = 20000;
const TIMEOUT_PER_DETAIL_MS = 8000;
const TIMEOUT_CAP_MS = 300000;

function timeoutForCastCount(count) {
  if (!Number.isSafeInteger(count) || count < 1) fail('SOURCE_MISSING');
  return Math.min(TIMEOUT_CAP_MS, TIMEOUT_BASE_MS + TIMEOUT_PER_DETAIL_MS * count);
}

function fail(code) { const error = new Error(code); error.code = code; throw error; }
function emitSafeDiagnostic(stage, errorClass, httpStatus, lastStage) {
  diagnosticWritten = true;
  const value = {stage, error_class: errorClass};
  if (Number.isInteger(httpStatus) && httpStatus >= 100 && httpStatus <= 599) value.http_status = httpStatus;
  if (/^(WEBSOCKET_(CONNECT|OPEN)|GIRL_LIST_PARSED|DETAIL_[0-9]{1,3}_(START|COMPLETE))$/.test(lastStage || '')) value.last_stage = lastStage;
  process.stderr.write('CITYHEAVEN_DAILY_DIAGNOSTIC:' + JSON.stringify(value) + '\n');
}
function candidateIdFromSource(source) {
  const match = String(source || '').match(/(?:member_id|girl_id)(?:=|%3D)(\d+)/i);
  return match ? match[1] : '';
}
function addCandidate(candidates, source, display, identify = candidateIdFromSource) {
  const girlId = identify(source);
  if (!girlId || candidates.has(girlId) || !display || display.length > 80) return false;
  candidates.set(girlId, display);
  return true;
}
function requireCandidates(candidates) {
  if (!candidates.size) fail('SOURCE_MISSING');
  return candidates;
}
function pageAuthState({requestedUrl, responseUrl, documentUrl, hasLoginForm, hasManagerStructure}) {
  // DOMParser inherits the caller's document URL. A fetched response can be
  // authenticated while the caller still displays the old login page.
  const urls = [responseUrl || documentUrl].filter(Boolean);
  const redirectedToLogin = urls.some(value => /\/C1Login\.php(?:[?#]|$)/i.test(String(value)));
  if (redirectedToLogin) return 'AUTH_REQUIRED';
  if (hasManagerStructure) return 'AUTHENTICATED';
  if (hasLoginForm) return 'AUTH_REQUIRED';
  return 'SOURCE_MISSING';
}
function headerPathsForTable(table) {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const headerRows = [];
  for (const row of table.querySelectorAll('tr')) {
    const cells = [...row.children];
    if (!cells.length) continue;
    // Daily rows use TH for the date; only the leading all-TH header block belongs here.
    if (cells.some(cell => cell.tagName !== 'TH'
        || /^(?:\d{1,2}\/\d{1,2}(?:[（(]|$)|[\d,.]+$)/.test(clean(cell.textContent)))) break;
    headerRows.push(row);
  }
  if (!headerRows.length) throw new Error('SCHEMA_DRIFT');
  const grid = [];
  headerRows.forEach((row, r) => {
    grid[r] ||= []; let c = 0;
    for (const cell of row.children) {
      while (grid[r][c]) c++;
      const rs = Number(cell.getAttribute('rowspan') || 1), cs = Number(cell.getAttribute('colspan') || 1);
      const text = clean(cell.textContent);
      for (let rr = r; rr < r + rs; rr++) { grid[rr] ||= []; for (let cc = c; cc < c + cs; cc++) grid[rr][cc] = text; }
      c += cs;
    }
  });
  const width = Math.max(...grid.map(row => row.length));
  const headerPaths = [];
  for (let c = 0; c < width; c++) headerPaths.push([...new Set(grid.map(row => row[c]).filter(Boolean))].join(' > '));
  return headerPaths;
}
function getJson(pathname) {
  return new Promise((resolve, reject) => {
    const req = http.get({host: '127.0.0.1', port: PORT, path: pathname, timeout: 3000}, response => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', chunk => { body += chunk; if (body.length > 2_000_000) req.destroy(); });
      response.on('end', () => { try { resolve(JSON.parse(body)); } catch { reject(new Error('BROWSER_UNAVAILABLE')); } });
    });
    req.on('timeout', () => req.destroy(new Error('BROWSER_UNAVAILABLE')));
    req.on('error', () => reject(new Error('BROWSER_UNAVAILABLE')));
  });
}

async function evaluate(target, expression) {
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  return await new Promise((resolve, reject) => {
    const started = Date.now();
    let lastStage = 'WEBSOCKET_CONNECT';
    const timedOut = () => {
      process.stderr.write('CITYHEAVEN_DAILY_LAST_STAGE=' + lastStage + '\n');
      try { ws.close(); } catch {}
      reject(new Error('BROWSER_UNAVAILABLE'));
    };
    let timer = setTimeout(timedOut, COUNT_DISCOVERY_TIMEOUT_MS);
    ws.onopen = () => { lastStage = 'WEBSOCKET_OPEN'; ws.send(JSON.stringify({id: 2, method: 'Runtime.enable'})); };
    ws.onerror = () => { clearTimeout(timer); reject(new Error('BROWSER_UNAVAILABLE')); };
    ws.onmessage = event => {
      const message = JSON.parse(event.data);
      if (message.method === 'Runtime.consoleAPICalled') {
        const value = message.params?.args?.[0]?.value;
        const match = /^CITYHEAVEN_DAILY_CAST_COUNT:([1-9]\d{0,3})$/.exec(String(value || ''));
        if (match) {
          const timeoutMs = timeoutForCastCount(Number(match[1]));
          lastStage = 'GIRL_LIST_PARSED';
          clearTimeout(timer);
          timer = setTimeout(timedOut, Math.max(1, timeoutMs - (Date.now() - started)));
          process.stderr.write('CITYHEAVEN_DAILY_TIMEOUT_MS=' + timeoutMs + '\n');
        }
        const detail = /^CITYHEAVEN_DAILY_DETAIL_(START|COMPLETE):([0-9]{1,3})$/.exec(String(value || ''));
        if (detail) lastStage = 'DETAIL_' + detail[2] + '_' + detail[1];
        return;
      }
      if (message.id === 2) {
        if (message.error) {
          clearTimeout(timer); try { ws.close(); } catch {}
          reject(new Error('BROWSER_UNAVAILABLE'));
          return;
        }
        ws.send(JSON.stringify({id: 1, method: 'Runtime.evaluate', params: {
          expression, returnByValue: true, awaitPromise: true,
        }}));
        return;
      }
      if (message.id !== 1) return;
      clearTimeout(timer);
      try { ws.close(); } catch {}
      if (message.result?.exceptionDetails) {
        const detail = JSON.stringify(message.result.exceptionDetails);
        const code = ['AUTH_REQUIRED', 'SCHEMA_DRIFT', 'SOURCE_MISSING'].find(item => detail.includes(item)) || 'SOURCE_MISSING';
        const stage = /\|STAGE=([A-Z_]+)/.exec(detail)?.[1] || 'BROWSER_EVALUATE';
        const errorClass = /\|CLASS=([A-Z_]+)/.exec(detail)?.[1] || 'BROWSER_EXCEPTION_UNCLASSIFIED';
        const httpStatus = Number(/\|HTTP=([0-9]{3})/.exec(detail)?.[1]);
        emitSafeDiagnostic(stage, errorClass, httpStatus, lastStage);
        reject(new Error(code));
      }
      else {
        process.stderr.write('CITYHEAVEN_DAILY_EVALUATE_ELAPSED_MS=' + (Date.now() - started) + '\n');
        resolve(message.result?.result?.value);
      }
    };
  });
}

async function main() {
  const targetDate = process.argv.slice(2).find(value => /^\d{4}-\d{2}-\d{2}$/.test(value || ''));
  if (!/^\d{4}-\d{2}-\d{2}$/.test(targetDate || '')) fail('SOURCE_MISSING');
  const targets = await getJson('/json');
  const target = targets.find(item => item.type === 'page' && /newmanager\.cityheaven\.net/.test(item.url || ''));
  if (!target) fail('BROWSER_UNAVAILABLE');
  const expression = `(${async function collect(targetDate, shopdir, classifyAuth, candidateId, putCandidate, makeHeaderPaths) {
    const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
    const sourceFailure = (code, stage, errorClass, status) => {
      throw new Error(code + '|STAGE=' + stage + '|CLASS=' + errorClass +
        (Number.isInteger(status) ? '|HTTP=' + status : ''));
    };
    const get = async url => {
      const prefix = /C2GirlList\.php/.test(url) ? 'GIRL_LIST' : 'GIRL_DETAIL';
      let response;
      try { response = await fetch(url, {method: 'GET', credentials: 'include', redirect: 'follow'}); }
      catch { sourceFailure('SOURCE_MISSING', prefix + '_HTTP', 'FETCH_EXCEPTION'); }
      if (!response.ok) sourceFailure('SOURCE_MISSING', prefix + '_HTTP', 'HTTP_NOT_OK', response.status);
      const html = await response.text();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const isGirlList = /\/C2GirlList\.php(?:[?#]|$)/i.test(url);
      const isGirlDetail = /\/C2TokeiGirlDetail\.php(?:[?#]|$)/i.test(url);
      const hasManagerStructure = isGirlList
        ? Boolean(doc.querySelector('form[action*="C2GirlList.php"],a[href*="member_id="],[onclick*="member_id"],form[action*="member_id"]'))
        : (isGirlDetail
          ? [...doc.querySelectorAll('table tr')].some(row => row.querySelectorAll('th').length >= 8)
          : false);
      const hasLoginForm = Boolean(doc.querySelector(
        'form[action*="C1Login.php"] input[type="password"],form[action*="C1Login.php"] input[name="txt_password"]'
      ));
      const authState = classifyAuth({
        requestedUrl: url,
        responseUrl: response.url,
        documentUrl: doc.URL,
        hasLoginForm,
        hasManagerStructure,
      });
      if (authState === 'AUTH_REQUIRED') sourceFailure('AUTH_REQUIRED', prefix + '_STRUCTURE', 'LOGIN_REDIRECT_OR_FORM');
      if (authState !== 'AUTHENTICATED') sourceFailure(authState, prefix + '_STRUCTURE', 'MANAGER_STRUCTURE_MISSING');
      return doc;
    };
    const list = await get('/C2GirlList.php?shopdir=' + encodeURIComponent(shopdir));
    const candidates = new Map();
    const displayName = (element, row) => {
      const named = row?.querySelector('.girl_name,.cast_name,[class*="girlName"],[class*="castName"],[class*="girl_name"],[class*="cast_name"]');
      const imageAlt = row?.querySelector('img[alt]')?.getAttribute('alt');
      const values = [named?.textContent, imageAlt, element?.textContent].map(clean).filter(Boolean);
      return values.find(value => value.length <= 80 && !/^(詳細|編集|確認|アクセス|日記|プロフィール|detail|edit)$/i.test(value)) || '';
    };
    for (const anchor of list.querySelectorAll('a[href]')) {
      const href = anchor.getAttribute('href') || '';
      const girlId = candidateId(href);
      if (!girlId) continue;
      const row = anchor.closest('tr,li,div');
      const display = displayName(anchor, row);
      putCandidate(candidates, href, display, candidateId);
    }
    // Historical C2GirlList variants put the id in form actions/onclick.
    for (const element of list.querySelectorAll('[onclick],form[action]')) {
      const source = element.getAttribute('onclick') || element.getAttribute('action') || '';
      const girlId = candidateId(source);
      if (!girlId || candidates.has(girlId)) continue;
      const row = element.closest('tr,li,div');
      const display = displayName(element, row);
      putCandidate(candidates, source, display, candidateId);
    }
    if (!candidates.size) sourceFailure('SOURCE_MISSING', 'GIRL_LIST_CANDIDATES', 'NO_CANDIDATES');
    console.log('CITYHEAVEN_DAILY_CAST_COUNT:' + candidates.size);

    const wanted = targetDate.slice(5, 7).replace(/^0/, '') + '/' + targetDate.slice(8, 10).replace(/^0/, '');
    const casts = [];
    for (const [girlId, displayName] of candidates) {
      const detailIndex = casts.length;
      console.log('CITYHEAVEN_DAILY_DETAIL_START:' + detailIndex);
      const detail = await get('/C2TokeiGirlDetail.php?member_id=' + girlId + '&shopdir=' + encodeURIComponent(shopdir));
      const tables = [...detail.querySelectorAll('table')];
      let chosen = null;
      for (const table of tables) {
        const rows = [...table.querySelectorAll('tr')];
        if (rows.some(row => clean(row.textContent).startsWith(wanted + '(')) && rows.some(row => row.querySelectorAll('th').length >= 8)) {
          chosen = table; break;
        }
      }
      if (!chosen) sourceFailure('SCHEMA_DRIFT', 'GIRL_DETAIL_TABLE', 'METRIC_TABLE_MISSING');
      const headerPaths = makeHeaderPaths(chosen);
      const row = [...chosen.querySelectorAll('tr')].find(item => clean(item.textContent).startsWith(wanted + '('));
      if (!row) sourceFailure('SOURCE_MISSING', 'GIRL_DETAIL_DAY_ROW', 'TARGET_DAY_ROW_MISSING');
      const cells = [...row.children].map(cell => clean(cell.textContent));
      const dateOffset = cells[0]?.startsWith(wanted) ? 1 : 0;
      casts.push({girl_id: girlId, display_name: displayName, header_paths: headerPaths.slice(dateOffset), values: cells.slice(dateOffset)});
      console.log('CITYHEAVEN_DAILY_DETAIL_COMPLETE:' + detailIndex);
    }
    return {target_date: targetDate, casts, source_pages: ['C2GirlList', 'C2TokeiGirlDetail'], read_only: true, cityheaven_write_count: 0};
  }.toString()})(${JSON.stringify(targetDate)}, ${JSON.stringify(SHOPDIR)}, (${pageAuthState.toString()}), (${candidateIdFromSource.toString()}), (${addCandidate.toString()}), (${headerPathsForTable.toString()}))`;
  const result = await evaluate(target, expression);
  if (!result || result.read_only !== true || result.cityheaven_write_count !== 0) {
    emitSafeDiagnostic('PAYLOAD_ENVELOPE', 'INVALID_READER_RESULT');
    fail('SOURCE_MISSING');
  }
  process.stdout.write(JSON.stringify(result));
}

if (require.main === module) main().catch(error => {
  const allowed = new Set(['AUTH_REQUIRED', 'BROWSER_UNAVAILABLE', 'SCHEMA_DRIFT', 'SOURCE_MISSING']);
  const code = allowed.has(error?.message) ? error.message : (allowed.has(error?.code) ? error.code : 'SOURCE_MISSING');
  if (!diagnosticWritten) emitSafeDiagnostic('READER_MAIN', code);
  process.stderr.write(code + '\n');
  process.exit(code === 'AUTH_REQUIRED' ? 20 : 30);
});

module.exports = { configuredPort, pageAuthState, candidateIdFromSource, addCandidate, requireCandidates, timeoutForCastCount, headerPathsForTable };
