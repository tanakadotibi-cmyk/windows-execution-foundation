'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const childProcess = require('node:child_process');

// Same fixed cancellation marker as the Foundation reservation entry.
const PAUSE_PATH = path.resolve(__dirname, '../../local/reservation-chat-pause.json');
function reservationPaused() {
  let fd;
  try {
    fd = fs.openSync(PAUSE_PATH, 'r');
    const buffer = Buffer.alloc(4097);
    let n = 0, got;
    while (n < buffer.length && (got = fs.readSync(fd, buffer, n, buffer.length - n, null)) > 0) n += got;
    if (n > 4096) return true;
    const v = JSON.parse(buffer.subarray(0, n).toString('utf8'));
    return !(v && Object.keys(v).length === 2 && v.scope === 'CITYHEAVEN_RESERVATION_SCHEDULE' && v.paused === false);
  } catch { return true; }
  finally { if (fd !== undefined) { try { fs.closeSync(fd); } catch {} } }
}
function writeJsonAtomic(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(value, null, 2) + '\n', { encoding: 'utf8', flag: 'wx' });
  fs.renameSync(temporary, file);
}

function readJson(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; }
}

function sendStatePath(manifest) {
  if (typeof manifest.send_state_path === 'string' && manifest.send_state_path) return manifest.send_state_path;
  if (typeof manifest.receipt_path === 'string' && manifest.receipt_path) return `${manifest.receipt_path}.send-state.json`;
  throw new Error('SEND_STATE_PATH_INVALID');
}

function sendLedgerState(manifest) {
  const statePath = sendStatePath(manifest);
  const value = readJson(statePath);
  if (value === null) return null;
  if (value?.schema_version !== 'cityheaven_send_state_v1'
    || value?.dedup_key !== manifest.reservation_key
    || !['SENDING', 'SENT'].includes(value?.state)) throw new Error('SEND_STATE_INVALID');
  return value;
}

function claimSending(manifest) {
  const statePath = sendStatePath(manifest);
  fs.mkdirSync(path.dirname(statePath), { recursive: true });
  const value = { schema_version: 'cityheaven_send_state_v1', dedup_key: manifest.reservation_key,
    state: 'SENDING', updated_at: new Date().toISOString() };
  try {
    fs.writeFileSync(statePath, JSON.stringify(value, null, 2) + '\n', {
      encoding: 'utf8', flag: 'wx',
    });
    return { claimed: true, state: value };
  } catch (error) {
    if (error?.code !== 'EEXIST') throw error;
    return { claimed: false, state: sendLedgerState(manifest) };
  }
}

function markSent(manifest) {
  const current = sendLedgerState(manifest);
  if (current?.state !== 'SENDING') throw new Error('SEND_STATE_INVALID');
  writeJsonAtomic(sendStatePath(manifest), { ...current, state: 'SENT', updated_at: new Date().toISOString() });
}

function staffReservationKey(rawStableKey) {
  return `ch_${crypto.createHash('sha256').update(rawStableKey, 'utf8').digest('hex')}`;
}

function defaultNotifyStaff(manifest, reservationKey, occurredAt) {
  if (typeof manifest.config_path !== 'string' || !manifest.config_path
    || typeof manifest.python_executable !== 'string' || !manifest.python_executable
    || typeof manifest.foundation_root !== 'string' || !manifest.foundation_root
    || typeof manifest.foundation_src !== 'string' || !manifest.foundation_src) {
    return { status: 'FAILED', reason_code: 'AUTH_REQUIRED_INTERNAL_TOKEN_NOT_PROVISIONED' };
  }
  const completed = childProcess.spawnSync(manifest.python_executable,
    ['-m', 'windows_execution_foundation.cityheaven_staff_notify', '--config', manifest.config_path,
      '--reservation-key', reservationKey, '--occurred-at', occurredAt,
      '--state-path', sendStatePath(manifest)], {
      cwd: manifest.foundation_root, env: { ...process.env, PYTHONPATH: manifest.foundation_src },
      encoding: 'utf8', timeout: 30000, windowsHide: true,
    });
  try {
    const value = JSON.parse(completed.stdout || '{}');
    if (typeof value?.status === 'string' && typeof value?.reason_code === 'string') return value;
  } catch { /* Sanitized failure below; never surface subprocess output. */ }
  return { status: 'FAILED', reason_code: 'STAFF_NOTIFY_RUNTIME_FAILED' };
}

const SAFE_TOP_LEVEL_REASON_CODES = new Set([
  'CHROME_RUNTIME_CONFIG_INVALID', 'CDP_RESPONSE_INVALID', 'CDP_TIMEOUT',
  'CDP_CONNECTION_FAILED', 'CDP_DISCONNECTED', 'CDP_COMMAND_FAILED',
  'CDP_DISCOVERY_FAILED', 'CDP_EVALUATION_FAILED', 'CDP_PORT_RUNTIME_MISMATCH',
  'CHROME_RUNTIME_UNAVAILABLE', 'BROWSER_LAUNCH_FAILED', 'CHROME_START_ALREADY_ATTEMPTED',
  'CHROME_START_LOCK_FAILED', 'CHROME_START_IN_PROGRESS', 'BROWSER_CDP_UNAVAILABLE',
  'BROWSER_KIND_INVALID', 'CITY_TARGET_UNAVAILABLE', 'CITY_DEDICATED_PAGE_UNAVAILABLE',
  'AUTH_REQUIRED', 'AUTH_REQUIRED_CMS', 'CITY_AUTH_TRANSFER_FAILED',
  'CITY_ROWS_INVALID', 'MANIFEST_INVALID', 'READER_INVALID', 'STOPPED',
  'POLICY_LOAD_FAILED', 'POLICY_IDENTITY_MISMATCH',
]);

function safeTopLevelReasonCode(error) {
  if (typeof error?.runtimeCode === 'string' && SAFE_TOP_LEVEL_REASON_CODES.has(error.runtimeCode)) {
    return error.runtimeCode;
  }
  if (typeof error?.message === 'string' && SAFE_TOP_LEVEL_REASON_CODES.has(error.message)) {
    return error.message;
  }
  return 'RUNTIME_UNCLASSIFIED';
}

function jstParts(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) throw new Error('RESERVATION_AT_INVALID');
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Tokyo', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(date).filter(part => part.type !== 'literal').map(part => [part.type, part.value]));
  return { date: `${parts.year}/${parts.month}/${parts.day}`, time: `${parts.hour}:${parts.minute}` };
}

function observedStatus(value) {
  const text = String(value ?? '').trim();
  if (text === '仮予約') return 'PROVISIONAL';
  if (text === '確定') return 'CONFIRMED';
  if (text === 'キャンセル' || text === '取消') return 'CANCELLED';
  return 'OTHER';
}

function normalizeMessageText(value) {
  let text = String(value ?? '');
  text = text.replace(/<br\s*\/?>/gi, '\n').replace(/<\/p\s*>/gi, '\n');
  text = text.replace(/<[^>]*>/g, '');
  text = text.replace(/&nbsp;|&#160;/gi, ' ').replace(/&amp;/gi, '&')
    .replace(/&lt;/gi, '<').replace(/&gt;/gi, '>').replace(/&quot;/gi, '"').replace(/&#39;/gi, "'");
  return text.replace(/\r\n?/g, '\n').split('\n').map(line => line.replace(/[\t \u3000]+/g, ' ').trim())
    .join('\n').replace(/\n{3,}/g, '\n\n').trim();
}

function chooseTemplate(course, policy) {
  const exact = typeof course === 'string' ? course.trim() : '';
  if (!exact || exact.toUpperCase() === 'UNKNOWN') throw new Error('COURSE_SOURCE_MISSING');
  return exact === policy.template_a.course_exact ? { key: 'A', text: policy.template_a.text }
    : { key: 'B', text: policy.template_b.text };
}

function decideMessage(messages, canonicalText) {
  if (!Array.isArray(messages)) throw new Error('CHAT_HISTORY_INVALID');
  const canonical = normalizeMessageText(canonicalText);
  const messageMatchCount = messages.reduce((count, message) => {
    const value = typeof message === 'string' ? message : message?.text ?? message?.body ?? message?.message;
    return count + (normalizeMessageText(value) === canonical ? 1 : 0);
  }, 0);
  return messages.length > 0
    ? { decision: 'DONE_NO_SEND', chatMessageCount: messages.length, messageMatchCount,
      messageState: 'DONE_NO_SEND', wouldSend: false }
    : { decision: 'WOULD_SEND', chatMessageCount: 0, messageMatchCount: 0,
      messageState: 'READY_TO_SEND', wouldSend: true };
}

// This gate only authorizes the existing staff notifier. It never authorizes
// customer delivery, and does not relax the legacy decideMessage contract.
function zonedInstant(value) {
  if (typeof value !== 'string'
    || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return NaN;
  const [year, month, day] = value.slice(0, 10).split('-').map(Number);
  if (month < 1 || month > 12 || day < 1
    || day > new Date(Date.UTC(year, month, 0)).getUTCDate()) return NaN;
  if (Number(value.slice(11, 13)) > 23 || Number(value.slice(14, 16)) > 59
    || Number(value.slice(17, 19)) > 59) return NaN;
  return Date.parse(value);
}

function staffTimeGate(reservationAt, now) {
  const start = zonedInstant(reservationAt);
  const checked = now instanceof Date ? now.getTime() : zonedInstant(now);
  if (!Number.isFinite(start) || !Number.isFinite(checked)) return 'UNKNOWN';
  if (jstParts(reservationAt).date === jstParts(new Date(checked)).date) return 'EXCLUDED';
  return start - checked < 30 * 60 * 1000 ? 'EXCLUDED' : 'ELIGIBLE';
}

function normalizeGuidance(value) {
  return normalizeMessageText(value).normalize('NFKC').replace(/[\s、，,。.!！]+/gu, '');
}

function decideStaffGuidance(history, policy, reservationId) {
  const unknown = { state: 'UNKNOWN', matchCount: 0 };
  if (!history || history.authenticated !== true || history.complete !== true
    || history.has_more === true || history.next_cursor
    || !Array.isArray(history.messages) || typeof reservationId !== 'string' || !reservationId) return unknown;
  // Take the opening sentence from each canonical template, not a second store.
  const openings = [policy?.template_a?.text, policy?.template_b?.text].map(text => {
    if (typeof text !== 'string') return '';
    const normalized = normalizeGuidance(text);
    const lead = normalized.match(/ネット予約ありがとうございます|この度はご予約いただきありがとうございます/u);
    if (lead) return lead[0];
    return normalizeGuidance(normalizeMessageText(text).split(/[\n。!！]/u)[0]);
  });
  if (openings.some(text => !text)) return unknown;
  let ambiguous = false;
  let matchCount = 0;
  for (const message of history.messages) {
    const text = typeof message === 'string' ? message : message?.text ?? message?.body ?? message?.message;
    if (typeof text !== 'string' || !text.trim()) { ambiguous = true; continue; }
    const lines = normalizeMessageText(text).split('\n');
    if (!lines.some((_line, index) => openings.some(opening =>
      normalizeGuidance(lines.slice(index).join('\n')).startsWith(opening)))) continue;
    const role = message?.sender_role ?? message?.senderRole;
    const linked = message?.rsvId ?? message?.reservation_id ?? message?.reservationId;
    if (role === 'customer') continue; // A customer's quotation is not staff delivery.
    if (typeof linked === 'string' && linked && linked !== reservationId) continue;
    if (role !== 'staff' || linked !== reservationId) { ambiguous = true; continue; }
    matchCount++;
  }
  return { state: matchCount ? 'PRESENT' : ambiguous ? 'UNKNOWN' : 'MISSING', matchCount };
}

async function primeCityDedicatedPage(reader, date) {
  if (typeof reader.read !== 'function') return false;
  return reader.read('city', function cityDedicatedPagePrime(targetDate) {
    if (location.origin !== 'https://pro-manager.cityheaven.net') return false;
    if (/login|signin/i.test(location.pathname)) return false;
    const target = `/reservation/list?date=${encodeURIComponent(targetDate)}&from_url=acpt0201`;
    if (location.pathname !== '/reservation/list' || location.search !== `?date=${encodeURIComponent(targetDate)}&from_url=acpt0201`) {
      history.replaceState(history.state, '', target);
    }
    return location.pathname === '/reservation/list';
  }, date);
}

function baseReceipt(manifest, reason, candidateCount) {
  return {
    schema_version: 'cityheaven_reservation_result_v1', reservation_key: manifest.reservation_key,
    request_hash: manifest.request_hash, terminal_status: 'FAILED', reason_code: reason,
    observed_at: new Date().toISOString(), reservation_at: manifest.reservation_at ?? 'UNKNOWN',
    cast: manifest.cast, course: manifest.course, candidate_count: candidateCount,
    observed_status: null, reservation_ref: null, course_source: null, policy_version: null,
    template_key: null, chat_match_count: 0, message_match_count: 0,
    message_state: 'UNDETERMINED', would_send: false, send_enabled: false, chat_read_count: 0,
    external_change_count: 0, reservation_mutation_count: 0, chat_open_count: 0,
    customer_send_count: 0, task_cleanup_status: 'PENDING',
    staff_notification_eligible: false, required_ab_state: 'UNKNOWN', time_gate_state: 'UNKNOWN',
  };
}

function writeTopLevelFailureReceipt(manifestPath, error) {
  if (typeof manifestPath !== 'string' || !manifestPath) return false;
  try {
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    const required = ['reservation_key', 'request_hash', 'reservation_at', 'cast', 'course', 'receipt_path'];
    if (!required.every(key => typeof manifest?.[key] === 'string' && manifest[key])) return false;
    if (fs.existsSync(manifest.receipt_path)) return false;
    const receipt = baseReceipt(manifest, safeTopLevelReasonCode(error), null);
    fs.mkdirSync(path.dirname(manifest.receipt_path), { recursive: true });
    fs.writeFileSync(manifest.receipt_path, JSON.stringify(receipt, null, 2) + '\n', {
      encoding: 'utf8', flag: 'wx',
    });
    return true;
  } catch {
    return false;
  }
}

function loadCanonicalPolicy(manifest, policy) {
  if (typeof manifest.policy_version !== 'string' || !manifest.policy_version) {
    throw new Error('POLICY_IDENTITY_MISMATCH');
  }
  if (!policy || typeof policy !== 'object' || Array.isArray(policy)) throw new Error('POLICY_JSON_INVALID');
  if (policy.schema_version !== 'cityheaven_ab_policy_v1') throw new Error('POLICY_SCHEMA_MISMATCH');
  if (policy.event_id !== 'evt_cityheaven_ab_message_canonical_20260925_v01'
    || policy.version !== manifest.policy_version) throw new Error('POLICY_IDENTITY_MISMATCH');
  if (!policy.rules || policy.rules.message_send_enabled !== false) throw new Error('POLICY_SEND_NOT_DISABLED');
  if (policy.rules.course_authority !== 'CITYHEAVEN_RESERVATION_DETAIL'
    || policy.rules.payload_course_is_hint_only !== true
    || policy.rules.unknown_course_must_fail_closed !== true
    || policy.rules.exact_message_match_required !== true) throw new Error('POLICY_RULES_INVALID');
  if (!policy.template_a || policy.template_a.match_type !== 'course_exact'
    || typeof policy.template_a.course_exact !== 'string' || !policy.template_a.course_exact.trim()
    || typeof policy.template_a.text !== 'string' || !policy.template_a.text.trim()
    || !policy.template_b || policy.template_b.match_type !== 'fallback_after_source_backed_course'
    || typeof policy.template_b.text !== 'string' || !policy.template_b.text.trim()) {
    throw new Error('POLICY_TEMPLATES_INVALID');
  }
  return policy;
}

async function run(manifestPath, dependencies = {}) {
  if (reservationPaused()) return 30; // Preserve job; no success/failed receipt.
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  const required = ['reservation_key', 'cast', 'course', 'request_hash', 'receipt_path', 'reader_path'];
  if (!required.every(key => typeof manifest[key] === 'string' && manifest[key])) throw new Error('MANIFEST_INVALID');
  if (!Number.isFinite(zonedInstant(manifest.reservation_at))) {
    const receipt = baseReceipt(manifest, 'RESERVATION_TIME_UNKNOWN', null);
    receipt.message_state = 'UNKNOWN';
    writeJsonAtomic(manifest.receipt_path, receipt);
    return 20;
  }
  const monitor = require(manifest.reader_path);
  if (typeof monitor.BrowserReader !== 'function') throw new Error('READER_INVALID');
  const Adapter = dependencies.Adapter || require('./cityheaven_chat_readonly_adapter').CityHeavenChatReadOnlyAdapter;
  const target = jstParts(manifest.reservation_at);
  const controller = new AbortController();
  try {
    const reader = dependencies.reader || new monitor.BrowserReader(controller.signal, manifest.browser_runtime);
    await reader.ensure('city');
    await primeCityDedicatedPage(reader, target.date);
    const rows = await reader.city(target.date);
    if (!Array.isArray(rows)) throw new Error('CITY_ROWS_INVALID');
    const candidates = rows.filter(row => String(row?.startTime ?? '').trim() === target.time
      && String(row?.cast ?? '').trim() === manifest.cast.trim());
    if (candidates.length !== 1) {
      writeJsonAtomic(manifest.receipt_path, baseReceipt(manifest,
        candidates.length === 0 ? 'CANDIDATE_NOT_FOUND' : 'CANDIDATE_AMBIGUOUS', candidates.length));
      return 20;
    }
    const row = candidates[0];
    if (typeof row.rsvId !== 'string' || !row.rsvId.trim()) {
      writeJsonAtomic(manifest.receipt_path, baseReceipt(manifest, 'RESERVATION_REF_MISSING', 1));
      return 20;
    }
    const receipt = baseReceipt(manifest, 'OBSERVED_READ_ONLY', 1);
    receipt.observed_status = observedStatus(row.status);
    receipt.reservation_ref = crypto.createHash('sha256').update(row.rsvId.trim(), 'utf8').digest('hex');
    if (receipt.observed_status !== 'CONFIRMED') {
      receipt.terminal_status = 'SUCCEEDED';
      writeJsonAtomic(manifest.receipt_path, receipt);
      return 0;
    }
    const checkedAt = dependencies.now ? dependencies.now() : new Date();
    receipt.time_gate_state = staffTimeGate(manifest.reservation_at, checkedAt);
    if (receipt.time_gate_state === 'UNKNOWN') {
      receipt.reason_code = 'RESERVATION_TIME_UNKNOWN';
      receipt.message_state = 'UNKNOWN';
      writeJsonAtomic(manifest.receipt_path, receipt);
      return 20;
    }
    if (receipt.time_gate_state === 'EXCLUDED') {
      receipt.reason_code = 'RESERVATION_WITHIN_30_MINUTES';
      receipt.terminal_status = 'SUCCEEDED';
      writeJsonAtomic(manifest.receipt_path, receipt);
      return 0;
    }
    const adapter = dependencies.adapter || new Adapter(reader);
    let detail;
    try { detail = await adapter.reservationDetail(row); } catch { detail = null; }
    receipt.course_source = 'RESERVATION_DETAIL';
    const detailCourse = typeof detail?.course === 'string' ? detail.course.trim() : '';
    if (!detailCourse || detailCourse.toUpperCase() === 'UNKNOWN') {
      receipt.reason_code = 'COURSE_SOURCE_MISSING';
      writeJsonAtomic(manifest.receipt_path, receipt);
      return 20;
    }
    let policy;
    try { policy = dependencies.loadPolicy ? await dependencies.loadPolicy()
      : loadCanonicalPolicy(manifest, dependencies.parentPolicy); }
    catch (error) {
      const safeCodes = new Set(['POLICY_RECORD_NOT_UNIQUE', 'POLICY_RECORD_INVALID', 'POLICY_IDENTITY_MISMATCH',
        'POLICY_JSON_INVALID', 'POLICY_SCHEMA_MISMATCH', 'POLICY_SEND_NOT_DISABLED', 'POLICY_RULES_INVALID',
        'POLICY_TEMPLATES_INVALID', 'POLICY_LOAD_FAILED']);
      receipt.reason_code = safeCodes.has(error?.message) ? error.message : 'POLICY_LOAD_FAILED';
      writeJsonAtomic(manifest.receipt_path, receipt);
      return 20;
    }
    receipt.policy_version = policy.version;
    const template = chooseTemplate(detailCourse, policy);
    receipt.template_key = template.key;
    let chatCandidates;
    try { chatCandidates = await adapter.chatCandidates(detail); } catch { chatCandidates = null; }
    receipt.chat_match_count = Array.isArray(chatCandidates) ? chatCandidates.length : 0;
    if (!Array.isArray(chatCandidates) || chatCandidates.length !== 1) {
      receipt.reason_code = 'CHAT_MATCH_UNDETERMINED';
      receipt.message_state = 'FAIL_CLOSED';
      writeJsonAtomic(manifest.receipt_path, receipt);
      return 20;
    }
    if (chatCandidates[0]?.unread !== false) {
      receipt.reason_code = 'CHAT_UNREAD_SIDE_EFFECT_RISK';
      receipt.message_state = 'FAIL_CLOSED';
      writeJsonAtomic(manifest.receipt_path, receipt);
      return 20;
    }
    let history;
    try { history = await adapter.chatHistory(chatCandidates[0]); } catch { history = null; }
    if (!history || !Array.isArray(history.messages)) {
      receipt.reason_code = 'CHAT_HISTORY_UNDETERMINED';
      receipt.message_state = 'FAIL_CLOSED';
      writeJsonAtomic(manifest.receipt_path, receipt);
      return 20;
    }
    receipt.chat_read_count = 1;
    receipt.chat_open_count = 1;
    const decision = decideStaffGuidance(history, policy, row.rsvId.trim());
    receipt.message_match_count = decision.matchCount;
    receipt.required_ab_state = decision.state;
    receipt.message_state = decision.state;
    // Hard disabled even if an injected/stale policy says true.
    receipt.would_send = false;
    receipt.send_enabled = false;
    if (decision.state === 'UNKNOWN') {
      receipt.reason_code = 'CHAT_GUIDANCE_UNDETERMINED';
      receipt.message_state = 'UNKNOWN';
      writeJsonAtomic(manifest.receipt_path, receipt);
      return 20;
    }
    if (decision.state === 'PRESENT') {
      receipt.reason_code = 'DONE_NO_SEND';
      receipt.terminal_status = 'SUCCEEDED';
      writeJsonAtomic(manifest.receipt_path, receipt);
      return 0;
    }
    receipt.staff_notification_eligible = true;
    {
      if (manifest.staff_notify_enabled !== true) {
        receipt.reason_code = 'SEND_DISABLED';
        receipt.terminal_status = 'SUCCEEDED';
        writeJsonAtomic(manifest.receipt_path, receipt);
        return 0;
      }
      const notificationKey = staffReservationKey(manifest.reservation_key);
      const occurredAt = new Date(checkedAt).toISOString();
      let notification;
      try {
        notification = dependencies.notifyStaff
          ? await dependencies.notifyStaff(notificationKey, occurredAt, sendStatePath(manifest))
          : defaultNotifyStaff(manifest, notificationKey, occurredAt);
      } catch { notification = { status: 'FAILED', reason_code: 'STAFF_NOTIFY_RUNTIME_FAILED' }; }
      const safeReasons = new Set([
        'STAFF_NOTIFIED', 'STAFF_ALREADY_NOTIFIED', 'STAFF_ALREADY_NOTIFIED_LOCAL',
        'AUTH_REQUIRED_INTERNAL_TOKEN_NOT_PROVISIONED', 'STAFF_NOTIFY_KEY_INVALID',
        'STAFF_NOTIFY_INPUT_INVALID', 'STAFF_NOTIFY_STATE_INVALID', 'STAFF_NOTIFY_UNCERTAIN',
        'STAFF_NOTIFY_NETWORK_FAILED', 'STAFF_NOTIFY_AUTH_FAILED', 'STAFF_NOTIFY_RATE_LIMITED',
        'STAFF_NOTIFY_UPSTREAM_FAILED', 'STAFF_NOTIFY_HTTP_FAILED',
        'STAFF_NOTIFY_RESPONSE_INVALID', 'STAFF_NOTIFY_RUNTIME_FAILED',
      ]);
      receipt.reason_code = safeReasons.has(notification?.reason_code)
        ? notification.reason_code : 'STAFF_NOTIFY_RUNTIME_FAILED';
      if (notification?.status === 'notified' || notification?.status === 'already_notified') {
        receipt.terminal_status = 'SUCCEEDED';
        writeJsonAtomic(manifest.receipt_path, receipt);
        return 0;
      }
      receipt.message_state = 'FAIL_CLOSED';
      writeJsonAtomic(manifest.receipt_path, receipt);
      return 20;
    }
  } finally { controller.abort(); }
}

function emitPreReaderPause() {
  console.log(JSON.stringify({schema:'reservation_pause_receipt/1', status:'PAUSED_BY_OWNER',
    scope:'CITYHEAVEN_RESERVATION_SCHEDULE', no_child_confirmed:true, stop_stage:'NODE_BEFORE_READER'}));
}
if (require.main === module && reservationPaused()) {
  emitPreReaderPause();
  process.exitCode = 30;
} else if (require.main === module) {
  let transport = null;
  try { transport = JSON.parse(fs.readFileSync(0, 'utf8') || '{}'); } catch { transport = null; }
  const parentPolicy = transport?.schema_version === 'cityheaven_policy_transport_v1'
    ? transport.policy : null;
  run(process.argv[2], { parentPolicy }).then(code => { if (code === 30) emitPreReaderPause(); process.exitCode = code; }).catch(error => {
    writeTopLevelFailureReceipt(process.argv[2], error);
    process.exitCode = 30;
  });
}

module.exports = { run, jstParts, observedStatus, normalizeMessageText, chooseTemplate, decideMessage,
  primeCityDedicatedPage, safeTopLevelReasonCode, writeTopLevelFailureReceipt,
  sendStatePath, sendLedgerState, claimSending, markSent, staffReservationKey, defaultNotifyStaff,
  loadCanonicalPolicy, zonedInstant, staffTimeGate, decideStaffGuidance };
