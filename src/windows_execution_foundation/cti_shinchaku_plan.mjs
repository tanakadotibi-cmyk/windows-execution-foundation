// Deterministic planning only. No network, browser connection, timer or writer.
// Browser observation functions run only through the official Chrome tool.
export function readTimelineDom() {
  const u = new URL(location.href);
  const route = u.hash.match(/^#([^/]+)\/schedule\?date=(\d{8})$/);
  // Match the reservation to the visible cast row, never open its detail panel.
  const rows = Array.from(document.querySelectorAll('.hime-rows > .schedule-row'))
    .filter(e => e.querySelector('hime-col .hime-name'))
    .map(e => {
      const col = e.querySelector('hime-col');
      const name = col.querySelector('.hime-name');
      const cast = Array.from(name.childNodes).filter(n => n.nodeType === 3)
        .map(n => n.textContent).join('').trim();
      const rect = e.getBoundingClientRect();
      return { cast, rowId: col.getAttribute('himeid'), shop: col.getAttribute('hdshop'),
        nameMatches: cast === col.getAttribute('himename'),
        top: rect.top, bottom: rect.bottom, height: rect.height };
    });
  return {
    readMode: 'TIMELINE_ROWS_V5',
    origin: u.origin, office: route?.[1] ?? null, businessDate: route?.[2] ?? null,
    castRows: rows.map(({ top, bottom, height, ...row }) => row),
    reservations: Array.from(document.querySelectorAll('apo-resv')).map(e => {
      const rect = e.getBoundingClientRect();
      const mid = (rect.top + rect.bottom) / 2;
      const matches = rows.filter(r => rect.height > 0 && r.height > 0 && mid >= r.top && mid < r.bottom &&
        rect.top >= r.top - 1 && rect.bottom <= r.bottom + 1);
      const row = matches.length === 1 && matches[0].nameMatches ? matches[0] : null;
      const states = Array.from(e.querySelectorAll('.mark-badge')).map(b => b.textContent.trim())
        .filter(s => ['入室', '待ち', '退室', '終了'].includes(s));
      return {
        reservationId: e.getAttribute('resv-id'), businessDate: e.getAttribute('date'),
        time: e.getAttribute('time'), visibleTime: e.querySelector('.resv-time')?.textContent.trim(),
        state: states.length === 1 ? states[0] : 'UNKNOWN',
        cast: row?.cast ?? null, rowId: row?.rowId ?? null, shop: row?.shop ?? null,
        rowMatched: !!row,
      };
    }),
  };
}

export function readTargetsDom() {
  const account = document.body.innerText.match(/こんにちは、\s*(.+?)\s*様/)?.[1] ?? null;
  const targets = Array.from(document.querySelectorAll('[id^="regist_girl_box_"]')).map(e => {
    const a = e.querySelector('a[href*="girl/edit.php?id="]');
    const href = a?.getAttribute('href');
    return { targetId: href?.match(/[?&]id=(\d+)/)?.[1] ?? null,
      name: a?.textContent.trim() ?? null,
      shift: e.querySelector('.shukkin-status')?.textContent.match(/(?:出勤中|出予定)\s*(\d{1,2}:\d{2})[～~](\d{1,2}:\d{2})/)?.slice(1) ?? null };
  });
  return { origin: location.origin, account, targets,
    remainingUpdates: Number(document.body.innerText.match(/本日の残り更新[：:]\s*(\d+)/)?.[1] ?? NaN) };
}

export function readApplicationDom() {
  return Array.from(document.querySelectorAll('[id^="regist_girl_box_"]')).map(e => {
    const targetId = e.id.replace('regist_girl_box_', '');
    const row = e.closest('tr');
    const command = row?.querySelector('.data-box-command');
    const button = e.querySelector('.update-btn');
    const application = command?.innerText?.trim() ?? '';
    return { targetId,
      application,
      // 「未設定」 belongs to the comment, not the publication status.
      initialUnset: !!command && application === '' &&
        button?.getAttribute('data-girlid') === targetId && button?.getAttribute('data-command') === '',
      processing: !!row?.querySelector('.regist_loding') };
  });
}

function epoch(date, text) {
  const d = /^(\d{4})(\d{2})(\d{2})$/.exec(date ?? '');
  const t = /^(翌)?(\d{1,2}):(\d{2})$/.exec(text ?? '');
  if (!d || !t || +t[2] > 29 || +t[3] > 59 || (t[1] && +t[2] >= 24)) throw Error('INVALID_DATE_TIME');
  const day = `${d[1]}-${d[2]}-${d[3]}`;
  const base = Date.parse(`${day}T00:00:00+09:00`);
  if (!Number.isFinite(base) || new Date(base + 9 * 3600000).toISOString().slice(0, 10) !== day) throw Error('INVALID_DATE_TIME');
  return base + ((t[1] ? 1440 : 0) + +t[2] * 60 + +t[3]) * 60000;
}

function jst(ms) { return new Date(ms + 9 * 3600000).toISOString().replace('Z', '+09:00'); }

function validTimelineRow(row, cti) {
  return cti.readMode === 'TIMELINE_ROWS_V5' && row.rowMatched === true && !!row.cast && !!row.rowId &&
    row.businessDate === cti.businessDate && row.time === row.visibleTime &&
    cti.castRows?.filter(r => r.rowId === row.rowId && r.cast === row.cast && r.nameMatches).length === 1 &&
    cti.castRows.filter(r => r.cast === row.cast).length === 1;
}

// Keep only the operational fields needed for comparison, never customer text.
export function timelineBaseline(snapshot) {
  const cti = snapshot?.cti;
  if (!Number.isFinite(Date.parse(snapshot?.observedAt)) || cti?.origin !== 'https://cti2.fuzoku-fan.jp' ||
      !cti.office || !/^\d{8}$/.test(cti.businessDate ?? '') || !cti.castRows?.length ||
      !Array.isArray(cti.reservations)) throw Error('TIMELINE_UNCONFIRMED');
  const ids = new Set();
  for (const row of cti.reservations) {
    if (!/^[A-Za-z0-9_-]+$/.test(row.reservationId ?? '') || ids.has(row.reservationId) ||
        !validTimelineRow(row, cti) || !['入室', '待ち', '退室', '終了'].includes(row.state)) throw Error('TIMELINE_UNCONFIRMED');
    ids.add(row.reservationId);
  }
  return { observedAt: snapshot.observedAt, cti: {
    origin: cti.origin, office: cti.office, businessDate: cti.businessDate, readMode: cti.readMode,
    castRows: cti.castRows.map(r => ({ cast: r.cast, rowId: r.rowId, shop: r.shop ?? null, nameMatches: r.nameMatches })),
    reservations: cti.reservations.map(r => ({ reservationId: r.reservationId, businessDate: r.businessDate,
      cast: r.cast, rowId: r.rowId, shop: r.shop ?? null, state: r.state,
      time: r.time, visibleTime: r.visibleTime, rowMatched: r.rowMatched })),
  } };
}

export function compareTimelines(snapshot, previous = null) {
  try {
    const nextBaseline = timelineBaseline(snapshot);
    if (!previous) return { mode: 'INITIAL_BASELINE', changes: [], nextBaseline };
    const before = timelineBaseline(previous);
    if (before.cti.origin !== nextBaseline.cti.origin || before.cti.office !== nextBaseline.cti.office)
      throw Error('BASELINE_SOURCE_MISMATCH');
    if (Date.parse(before.observedAt) >= Date.parse(nextBaseline.observedAt)) throw Error('NON_INCREASING_OBSERVATION');
    if (before.cti.businessDate !== nextBaseline.cti.businessDate)
      return { mode: 'BUSINESS_DATE_CHANGED', changes: [], nextBaseline };
    const oldRows = new Map(before.cti.reservations.map(r => [r.reservationId, r]));
    const newRows = new Map(nextBaseline.cti.reservations.map(r => [r.reservationId, r]));
    const fields = ['cast', 'rowId', 'shop', 'state', 'time'];
    const changes = [];
    for (const row of newRows.values()) {
      const old = oldRows.get(row.reservationId);
      if (!old) changes.push({ reservationId: row.reservationId, type: 'ADDED', before: null, after: row });
      else {
        const changedFields = fields.filter(f => old[f] !== row[f]);
        if (changedFields.length) changes.push({ reservationId: row.reservationId, type: 'CHANGED', changedFields, before: old, after: row });
      }
    }
    for (const row of oldRows.values()) if (!newRows.has(row.reservationId))
      changes.push({ reservationId: row.reservationId, type: 'REMOVED', before: row, after: null });
    return { mode: 'COMPARED', changes, nextBaseline };
  } catch (error) { return { mode: 'HELD', reason: error.message, changes: [], nextBaseline: null }; }
}

// Select by freshly rendered clock label, never by a remembered relative value.
export function matchingTimeOption(options, candidate) {
  const [h, m] = candidate.hhmm.split(':').map(Number);
  const found = options.filter(o => {
    const match = /^(\d{1,2})時(\d{2})分/.exec(o.label);
    return match && +match[1] === h && +match[2] === m && o.value !== '';
  });
  if (found.length !== 1) throw Error('EXACT_VISIBLE_TIME_OPTION_REQUIRED');
  return found[0];
}

const HOUR = 60 * 60000;
const verifiedApplication = r => !['PENDING', 'UNKNOWN'].includes(r.result) &&
  (r.result === 'VERIFIED' || r.applicationResult === 'VERIFIED');
const isImmediate = r => r.mode === 'NOW' || ['HEAVEN_WAITING', 'WAITING_RENEWAL'].includes(r.rule);
const excluded = (row, config) => !!config.excludedCasts?.[row.cast] ||
  Object.values(config.excludedCasts ?? {}).some(e => e.ctiCastId === row.rowId);
const inShop = (shop, config) => !shop || shop.split(/[,、]/).map(s => s.trim()).includes(config.shop);

// Bind only an independently verified exact name pair to a freshly observed row ID.
export function reconcileCastMappings(cti, config) {
  if (cti.origin !== 'https://cti2.fuzoku-fan.jp' || cti.office !== config.office) throw Error('SOURCE_OR_ACCOUNT_MISMATCH');
  const ctiCastIds = { ...config.ctiCastIds }, added = [], held = [];
  for (const [cast, name] of Object.entries(config.castAliases)) {
    const rows = cti.castRows.filter(r => r.cast === cast);
    if (!rows.length || rows.some(r => excluded(r, config))) continue;
    if (rows.length !== 1 || !rows[0].nameMatches || !rows[0].rowId || !inShop(rows[0].shop, config) ||
        !config.targetIds[name] || cti.castRows.filter(r => config.castAliases[r.cast] === name).length !== 1) {
      held.push({ cast, reason: 'CAST_MAPPING_AMBIGUOUS' }); continue;
    }
    if (ctiCastIds[name] && ctiCastIds[name] !== rows[0].rowId) {
      held.push({ cast, reason: 'CTI_CAST_ID_CHANGED' }); continue;
    }
    if (!ctiCastIds[name]) { ctiCastIds[name] = rows[0].rowId; added.push({ cast, name, rowId: rows[0].rowId, targetId: config.targetIds[name] }); }
  }
  return { ctiCastIds, added, held };
}

// Reservation blocks apply from the moment they are entered, regardless of badges.
export function findAvailability(cti, target, config, now) {
  const rowId = config.ctiCastIds?.[target.name];
  const rows = cti.castRows.filter(r => r.rowId === rowId && config.castAliases[r.cast] === target.name &&
    r.nameMatches && !excluded(r, config) && inShop(r.shop, config));
  if (!rowId || rows.length !== 1) throw Error('CTI_CAST_ID_UNCONFIRMED');
  if (!Array.isArray(target.shift) || target.shift.length !== 2) throw Error('TARGET_SHIFT_UNCONFIRMED');
  const shiftStart = epoch(cti.businessDate, target.shift[0]);
  let shiftEnd = epoch(cti.businessDate, target.shift[1]);
  if (shiftEnd <= shiftStart) shiftEnd += 86400000;
  if (now < shiftStart || now >= shiftEnd) throw Error('OUTSIDE_TARGET_SHIFT');
  if (config.rounding !== 'CEIL_10_MINUTES') throw Error('ROUNDING_NOT_APPROVED');
  const blocks = cti.reservations.filter(r => r.rowId === rowId).map(r => {
    if (!validTimelineRow(r, cti) || !inShop(r.shop, config)) throw Error('TIMELINE_ROW_UNCONFIRMED');
    const parts = r.time.split('-');
    if (parts.length !== 2) throw Error('INVALID_DATE_TIME');
    const start = epoch(cti.businessDate, parts[0]), end = epoch(cti.businessDate, parts[1]);
    if (end <= start || end - start > 18 * HOUR) throw Error('INVALID_INTERVAL');
    return { start, end };
  }).sort((a, b) => a.start - b.start || a.end - b.end);
  let cursor = now;
  const gap = end => {
    // Check again after rounding so a nominal 60-minute gap cannot shrink below 60.
    const start = cursor === now ? now : Math.ceil(cursor / 600000) * 600000;
    const limit = Math.min(end, shiftEnd);
    return limit - start >= HOUR ? { mode: start === now ? 'NOW' : 'NEXT',
      availableAt: jst(start), availableUntil: jst(limit),
      targetEnd: start === now ? null : jst(start), hhmm: start === now ? null : jst(start).slice(11, 16) } : null;
  };
  for (const block of blocks) {
    if (block.end <= cursor) continue;
    if (block.start > cursor) { const found = gap(block.start); if (found) return found; }
    cursor = Math.max(cursor, block.end);
    if (cursor >= shiftEnd) return null;
  }
  return gap(shiftEnd);
}

// Cheap trigger selection can run without reading 新着ん when there is no work.
export function availabilityTriggers(snapshot, config, receipts = [], now = Date.now()) {
  const held = [], targetIds = new Set();
  const finish = (extra = {}) => ({ targetIds: [...targetIds], held, ...extra });
  if (config.productionState === 'PAUSED_BY_USER') return finish({ held: [{ reason: 'PAUSED_BY_USER' }] });
  if (config.availabilityRule?.enabled !== true || config.availabilityRule.minimumGapMinutes !== 60 ||
      config.availabilityRule.immediateValidityMinutes !== 60) return finish({ held: [{ reason: 'AVAILABILITY_RULE_UNCONFIRMED' }] });
  const age = now - Date.parse(snapshot.observedAt);
  if (!Number.isFinite(age) || age < 0 || age > 120000) return finish({ held: [{ reason: 'STALE_OBSERVATION' }] });
  if (snapshot.cti?.origin !== 'https://cti2.fuzoku-fan.jp' || snapshot.cti.office !== config.office)
    return finish({ held: [{ reason: 'SOURCE_OR_ACCOUNT_MISMATCH' }] });
  const comparison = compareTimelines(snapshot, config.previousTimeline);
  if (comparison.mode === 'HELD') return finish({ held: [{ reason: comparison.reason }] });
  if (comparison.mode !== 'COMPARED') return finish({ comparison });
  const addRow = row => {
    if (!row || excluded(row, config) || !inShop(row.shop, config)) return;
    const name = config.castAliases[row.cast];
    if (config.ctiCastIds?.[name] === row.rowId && config.targetIds?.[name]) targetIds.add(config.targetIds[name]);
  };
  for (const change of comparison.changes) {
    if (change.type === 'CHANGED' && change.changedFields.every(f => f === 'state')) continue;
    addRow(change.before); addRow(change.after);
  }
  for (const targetId of Object.values(config.targetIds)) {
    const mappedRow = snapshot.cti.castRows.find(r => config.targetIds[config.castAliases[r.cast]] === targetId);
    if (mappedRow && excluded(mappedRow, config)) continue;
    // First publication for an observed cast is retried until their shift starts.
    if (config.availabilityRule.initializeUnpublished && mappedRow &&
        !receipts.some(r => r.targetId === targetId && (r.businessDate === snapshot.cti.businessDate ||
          r.key?.split('|').includes(snapshot.cti.businessDate)))) addRow(mappedRow);
    const history = receipts.filter(r => r.targetId === targetId &&
      (r.rule === 'AVAILABILITY' || isImmediate(r)));
    const sameDay = history.filter(r => r.businessDate === snapshot.cti.businessDate ||
      r.key?.split('|').includes(snapshot.cti.businessDate));
    const latest = sameDay.filter(r => Number.isFinite(Date.parse(r.submittedAt)))
      .sort((a, b) => Date.parse(b.submittedAt) - Date.parse(a.submittedAt))[0];
    if (!latest || !verifiedApplication(latest)) continue;
    const due = isImmediate(latest) ? Date.parse(latest.submittedAt) + HOUR : Date.parse(latest.targetEnd);
    if (Number.isFinite(due) && now >= due) targetIds.add(targetId);
  }
  return finish({ comparison });
}

export function planAvailabilityUpdates(snapshot, config, receipts = [], now = Date.now()) {
  const triggers = availabilityTriggers(snapshot, config, receipts, now);
  const candidates = [], held = [...triggers.held], skipped = [];
  const finish = () => ({ candidates, held, skipped, comparison: triggers.comparison });
  if (held.length || !triggers.targetIds.length) return finish();
  if (snapshot.target?.origin !== 'https://si9.jp' || snapshot.target.account !== config.shop) {
    held.push({ reason: 'SOURCE_OR_ACCOUNT_MISMATCH' }); return finish();
  }
  for (const targetId of triggers.targetIds) {
    try {
      const targets = snapshot.target.targets.filter(t => t.targetId === targetId && config.targetIds[t.name] === targetId);
      if (targets.length !== 1 || snapshot.target.targets.filter(t => t.name === targets[0].name).length !== 1)
        throw Error('TARGET_ID_UNCONFIRMED');
      const target = targets[0];
      const views = (snapshot.application ?? []).filter(v => v.targetId === targetId);
      if (views.length !== 1 || (!/^(待機中|接客中|受付終了)(?:\s|$)/.test(views[0].application?.trim() ?? '') &&
          !(views[0].initialUnset === true && views[0].application === '')))
        throw Error('APPLICATION_STATE_UNCONFIRMED');
      if (views[0].processing) throw Error('APPLICATION_PROCESSING');
      const prior = receipts.filter(r => r.targetId === targetId);
      if (prior.some(r => !verifiedApplication(r) &&
          (['PENDING', 'UNKNOWN'].includes(r.result) || !prior.some(later => verifiedApplication(later) &&
            Date.parse(later.submittedAt) > Date.parse(r.submittedAt))))) throw Error('PREVIOUS_SEND_REQUIRES_RECONCILIATION');
      if (prior.some(r => !Number.isFinite(Date.parse(r.submittedAt)) || Date.parse(r.submittedAt) > now))
        throw Error('PREVIOUS_SEND_TIME_UNCONFIRMED');
      const slot = findAvailability(snapshot.cti, target, config, now) ??
        { mode: 'CLOSED', availableAt: null, availableUntil: null, targetEnd: null, hhmm: null };
      const siteIds = slot.mode === 'NOW' ? config.availabilityRule.immediateSiteIds :
        slot.mode === 'NEXT' ? config.availabilityRule.nextSiteIds : config.availabilityRule.closedSiteIds;
      if (!siteIds?.length || new Set(siteIds).size !== siteIds.length) throw Error('SITE_IDS_UNCONFIRMED');
      if (!Number.isFinite(snapshot.target.remainingUpdates) || snapshot.target.remainingUpdates < siteIds.length)
        throw Error('INSUFFICIENT_UPDATE_QUOTA');
      const latest = [...prior].sort((a, b) => Date.parse(b.submittedAt) - Date.parse(a.submittedAt))[0];
      const sameDay = latest && (latest.businessDate === snapshot.cti.businessDate || latest.key?.split('|').includes(snapshot.cti.businessDate));
      const sameMode = slot.mode === 'NOW' ? isImmediate(latest ?? {}) : latest?.mode === slot.mode;
      const sameSites = latest?.siteIds && [...latest.siteIds].sort().join('|') === [...siteIds].sort().join('|');
      const stillValid = latest && now - Date.parse(latest.submittedAt) < HOUR;
      if (sameDay && sameMode && sameSites &&
          (slot.mode === 'NOW' ? stillValid : latest.targetEnd === slot.targetEnd)) {
        skipped.push({ targetId, reason: 'DISPLAY_ALREADY_CURRENT' }); continue;
      }
      // The preceding receipt identifies the display being replaced; stable across retries.
      const key = ['AVAILABILITY', config.office, snapshot.cti.businessDate, targetId, slot.mode,
        slot.targetEnd ?? slot.mode, latest?.submittedAt ?? 'FIRST'].join('|');
      candidates.push({ rule: 'AVAILABILITY', key, businessDate: snapshot.cti.businessDate,
        targetId, targetCast: target.name, ...slot, siteIds: [...siteIds],
        status: slot.mode === 'NOW' ? '待機中（即ヒメ）' : slot.mode === 'NEXT' ? '接客中' : '受付終了',
        validityMinutes: slot.mode === 'NOW' ? 60 : null, observedAt: snapshot.observedAt });
    } catch (error) {
      if (error.message === 'OUTSIDE_TARGET_SHIFT') skipped.push({ targetId, reason: error.message });
      else held.push({ targetId, reason: error.message });
    }
  }
  return finish();
}

