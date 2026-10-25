'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const core = require('../src/windows_execution_foundation/cityheaven_reservation_once');
const { CityHeavenChatReadOnlyAdapter } = require('../src/windows_execution_foundation/cityheaven_chat_readonly_adapter');

const policy = { version: 'fixture', rules: { message_send_enabled: false },
  template_a: { course_exact: 'fixture-course', text: 'ネット予約ありがとうございます。\n予約詳細' },
  template_b: { text: 'この度はご予約いただき、ありがとうございます。\n予約詳細' } };
const now = '2026-09-29T14:00:00+09:00';
const full = messages => ({ messages, complete: true, authenticated: true });
const staff = text => ({ text, sender_role: 'staff', reservation_id: 'fixture-reservation' });

async function runFixture(options = {}) {
  const directory = options.directory || fs.mkdtempSync(path.join(os.tmpdir(), 'required-ab-'));
  const readerPath = path.join(directory, 'reader.js');
  fs.writeFileSync(readerPath, 'exports.BrowserReader=class {};\n');
  const receiptPath = path.join(directory, 'receipt.json');
  const manifestPath = path.join(directory, 'manifest.json');
  const start = Object.hasOwn(options, 'start') ? options.start : '2026-09-30T14:30:00+09:00';
  const manifest = { reservation_key: 'fixture-stable-key', reservation_at: start,
    cast: 'fixture-cast', course: 'hint', request_hash: 'fixture-hash',
    reader_path: readerPath, receipt_path: receiptPath,
    send_state_path: path.join(directory, 'notify-state.json'), staff_notify_enabled: true };
  fs.writeFileSync(manifestPath, JSON.stringify(manifest));
  let notified = 0;
  let sends = 0;
  let mutations = 0;
  const reader = { async ensure() {}, async city() { return [{
    startTime: Number.isFinite(core.zonedInstant(start)) ? core.jstParts(start).time : '14:30',
    cast: manifest.cast, rsvId: 'fixture-reservation', status: options.status || '確定',
  }]; } };
  const adapter = {
    async reservationDetail() { return { course: 'fixture-course' }; },
    async chatCandidates() { return options.chats ?? [{ unread: false }]; },
    async chatHistory() {
      if (options.fail) throw new Error('fixture timeout');
      return Object.hasOwn(options, 'history') ? options.history : full([]);
    },
    async sendMessage() { sends++; throw new Error('forbidden'); },
    async save() { mutations++; throw new Error('forbidden'); },
  };
  try {
    const code = await core.run(manifestPath, { reader, adapter,
      loadPolicy: async () => options.policy || policy, now: () => options.now ?? now,
      notifyStaff: async (...args) => { notified++; return options.notify
        ? options.notify(...args) : { status: 'notified', reason_code: 'STAFF_NOTIFIED' }; } });
    const receipt = JSON.parse(fs.readFileSync(receiptPath, 'utf8'));
    assert.equal(sends, 0);
    assert.equal(mutations, 0);
    assert.equal(receipt.customer_send_count, 0);
    assert.equal(receipt.reservation_mutation_count, 0);
    assert.equal(receipt.send_enabled, false);
    assert.equal(receipt.would_send, false);
    return { code, notified, receipt };
  } finally {
    if (!options.directory) fs.rmSync(directory, { recursive: true, force: true });
  }
}

const cases = [
  ['normal A', { history: full([staff(policy.template_a.text)]) }, 0, 'PRESENT'],
  ['normal B regardless of course', { history: full([staff(policy.template_b.text)]) }, 0, 'PRESENT'],
  ['normalized A with appended details', { history: full([staff('<p>ネット 予約ありがとうございます！</p><p>別の予約詳細</p>')]) }, 0, 'PRESENT'],
  ['normalized B punctuation', { history: full([staff('この度はご予約いただき, ありがとうございます。追加詳細')]) }, 0, 'PRESENT'],
  ['salutation without hardcoded name', { history: full([staff('架空のお客様へ\nネット予約ありがとうございます。詳細')]) }, 0, 'PRESENT'],
  ['line wrapped guidance', { history: full([staff('宛先\nネット予約ありがとうござい\nます。詳細')]) }, 0, 'PRESENT'],
  ['other staff message', { history: full([staff('集合場所について')]) }, 1, 'MISSING'],
  ['other customer message', { history: full([{ text: '質問です', sender_role: 'customer' }]) }, 1, 'MISSING'],
  ['complete empty', {}, 1, 'MISSING'],
  ['customer quoted A', { history: full([{ ...staff(policy.template_a.text), sender_role: 'customer' }]) }, 1, 'MISSING'],
  ['prior reservation A', { history: full([{ ...staff(policy.template_a.text), reservation_id: 'prior-reservation' }]) }, 1, 'MISSING'],
  ['staff A missing reservation metadata', { history: full([{ text: policy.template_a.text, sender_role: 'staff' }]) }, 0, 'UNKNOWN'],
  ['A missing sender metadata', { history: full([{ text: policy.template_a.text, reservation_id: 'fixture-reservation' }]) }, 0, 'UNKNOWN'],
  ['failed history', { fail: true }, 0, 'UNKNOWN'],
  ['null history', { history: null }, 0, 'UNKNOWN'],
  ['bare empty array', { history: [] }, 0, 'UNKNOWN'],
  ['incomplete history', { history: { ...full([]), complete: false } }, 0, 'UNKNOWN'],
  ['login expired', { history: { ...full([]), authenticated: false } }, 0, 'UNKNOWN'],
  ['more history pages', { history: { ...full([]), has_more: true } }, 0, 'UNKNOWN'],
  ['next cursor', { history: { ...full([]), next_cursor: 'fixture-next' } }, 0, 'UNKNOWN'],
  ['malformed entry not dropped', { history: full([{}]) }, 0, 'UNKNOWN'],
  ['recipient missing', { chats: [] }, 0, 'UNKNOWN'],
  ['recipient ambiguous', { chats: [{ unread: false }, { unread: false }] }, 0, 'UNKNOWN'],
  ['unread side effect', { chats: [{ unread: true }] }, 0, 'UNKNOWN'],
  ['provisional', { status: '仮予約' }, 0, 'UNKNOWN'],
  ['cancelled', { status: 'キャンセル' }, 0, 'UNKNOWN'],
  ['29m59s across JST midnight', { start: '2026-09-30T00:00:00+09:00', now: '2026-09-29T23:30:01+09:00' }, 0, 'UNKNOWN'],
  ['exactly 30m across JST midnight', { start: '2026-09-30T00:00:00+09:00', now: '2026-09-29T23:30:00+09:00' }, 1, 'MISSING'],
  ['32m across JST midnight', { start: '2026-09-30T00:00:00+09:00', now: '2026-09-29T23:28:00+09:00' }, 1, 'MISSING'],
  ['same JST day far ahead', { start: '2026-09-29T23:00:00+09:00' }, 0, 'UNKNOWN'],
  ['manager example checked 14:15', { now: '2026-09-30T14:15:00+09:00' }, 0, 'UNKNOWN'],
  ['already started', { now: '2026-09-30T14:31:00+09:00' }, 0, 'UNKNOWN'],
  ['start unknown', { start: 'UNKNOWN' }, 0, 'UNKNOWN'],
  ['start null', { start: null }, 0, 'UNKNOWN'],
  ['start timezone missing', { start: '2026-09-29T14:30:00' }, 0, 'UNKNOWN'],
  ['invalid calendar date', { start: '2026-02-30T14:30:00+09:00' }, 0, 'UNKNOWN'],
  ['invalid hour', { start: '2026-09-29T24:00:00+09:00' }, 0, 'UNKNOWN'],
  ['check time invalid', { now: 'unknown' }, 0, 'UNKNOWN'],
  ['policy true cannot send customer', { policy: { ...policy, rules: { message_send_enabled: true } } }, 1, 'MISSING'],
];
for (const [name, options, count, state] of cases) {
  test(name, async () => {
    const result = await runFixture(options);
    assert.equal(result.notified, count);
    assert.equal(result.receipt.required_ab_state, state);
  });
}

test('existing Python notifier dedup and uncertain replay, mock transport only', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'ab-dedup-'));
  const fixtureTransport = (key, at, state) => {
    const script = `import json,sys\nfrom pathlib import Path\nfrom windows_execution_foundation import cityheaven_staff_notify as n\nn._load_internal_token=lambda _: 'fixture-token'\ndef transport(req,timeout):\n payload=json.loads(req.data)\n assert set(payload)=={'event','reservation_key','occurred_at'}\n assert payload['event']=='cityheaven_unreplied'\n with Path(sys.argv[4]).open('a') as f: f.write('mock-call\\n')\n return 200,b'{"status":"notified"}'\nprint(json.dumps(n.notify(Path('unused'),sys.argv[1],sys.argv[2],Path(sys.argv[3]),transport=transport)))`;
    const result = spawnSync(process.env.CITYHEAVEN_TEST_PYTHON, ['-c', script, key, at, state,
      path.join(directory, 'calls.txt')], { encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    return JSON.parse(result.stdout);
  };
  try {
    const first = await runFixture({ directory, notify: fixtureTransport });
    const replay = await runFixture({ directory, notify: fixtureTransport });
    assert.equal(first.receipt.reason_code, 'STAFF_NOTIFIED');
    assert.equal(replay.receipt.reason_code, 'STAFF_ALREADY_NOTIFIED_LOCAL');
    assert.equal(fs.readFileSync(path.join(directory, 'calls.txt'), 'utf8').replace(/\r\n/g, '\n'), 'mock-call\n');
    const stateFile = path.join(directory, 'notify-state.json');
    const state = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
    fs.writeFileSync(stateFile, JSON.stringify({ ...state, state: 'SENDING' }));
    const uncertain = await runFixture({ directory, notify: fixtureTransport });
    assert.equal(uncertain.receipt.reason_code, 'STAFF_NOTIFY_UNCERTAIN');
    assert.equal(fs.readFileSync(path.join(directory, 'calls.txt'), 'utf8').replace(/\r\n/g, '\n'), 'mock-call\n');
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test('adapter retains JSON metadata and rejects redirect/bare completeness', async () => {
  const originalFetch = global.fetch;
  const originalLocation = global.location;
  const originalDOMParser = global.DOMParser;
  const adapter = new CityHeavenChatReadOnlyAdapter({ read: async (_kind, fn, arg) => fn(arg) });
  global.location = { origin: 'https://fixture.example' };
  try {
    const value = full([staff(policy.template_a.text)]);
    global.fetch = async () => ({ ok: true, text: async () => JSON.stringify(value) });
    const descriptor = { url: 'https://fixture.example/DisplayChat', method: 'GET' };
    const history = await adapter.chatHistory(descriptor);
    assert.deepEqual(history.messages, value.messages);
    assert.equal(core.decideStaffGuidance(history, policy, 'fixture-reservation').state, 'PRESENT');
    global.fetch = async () => ({ ok: true, text: async () => '[]' });
    assert.equal((await adapter.chatHistory(descriptor)).complete, false);
    global.fetch = async () => ({ ok: true, redirected: true, text: async () => '[]' });
    assert.equal(await adapter.chatHistory(descriptor), null);
    global.DOMParser = class { parseFromString() { return { querySelectorAll: () => [] }; } };
    global.fetch = async () => ({ ok: true, text: async () => '<html>login or incomplete page</html>' });
    const html = await adapter.chatHistory(descriptor);
    assert.equal(core.decideStaffGuidance(html, policy, 'fixture-reservation').state, 'UNKNOWN');
    await assert.rejects(adapter.sendMessage({}, 'fixture'), /CUSTOMER_SEND_DISABLED/);
  } finally { global.fetch = originalFetch; global.location = originalLocation; global.DOMParser = originalDOMParser; }
});
