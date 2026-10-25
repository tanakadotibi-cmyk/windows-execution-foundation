'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {setTimeout: delay} = require('node:timers/promises');
const pageSource = fs.readFileSync(path.join(__dirname, 'daily_saved_login_page.js'), 'utf8');
const logoutMessages = new Set(['ログアウトしました。もう一度ログインしてください。', 'ログアウトしました。']);

function connect(url, evidence) {
  const socket = new WebSocket(url);
  let sequence = 0;
  const pending = new Map();
  const ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => {socket.close(); reject(new Error('AUTH_CONNECTION_FAILED'));}, 3000);
    socket.onopen = () => {clearTimeout(timer); resolve();};
    socket.onerror = () => {clearTimeout(timer); reject(new Error('AUTH_CONNECTION_FAILED'));};
  });
  const call = async (method, params = {}) => {
    await ready;
    return new Promise((resolve, reject) => {
      const id = ++sequence;
      const timer = setTimeout(() => { pending.delete(id); reject(new Error('AUTH_COMMAND_TIMEOUT')); }, 5000);
      pending.set(id, {resolve, reject, timer});
      socket.send(JSON.stringify({id, method, params}));
    });
  };
  socket.onmessage = async event => {
    const item = JSON.parse(event.data);
    if (item.method === 'Page.javascriptDialogOpening') {
      if (item.params.type === 'alert' && logoutMessages.has(item.params.message)
          && evidence.logout_notice_ack_count === 0 && evidence.submit_count === 0) {
        evidence.logout_notice_ack_count = 1;
        try { await call('Page.handleJavaScriptDialog', {accept: true}); }
        catch { evidence.blocked = 'LOGOUT_ACK_FAILED'; }
      } else evidence.blocked = 'OWNER_AUTH_REQUIRED';
    }
    if (item.method === 'Network.responseReceived' && item.params.type === 'Document'
        && [401, 403, 429].includes(item.params.response.status)) evidence.blocked = 'ACCESS_RESTRICTED';
    const waiter = pending.get(item.id);
    if (!waiter) return;
    clearTimeout(waiter.timer); pending.delete(item.id);
    if (item.error) waiter.reject(new Error('AUTH_COMMAND_FAILED'));
    else waiter.resolve(item.result);
  };
  return {call, close: () => {
    for (const value of pending.values()) {clearTimeout(value.timer); value.reject(new Error('AUTH_CONNECTION_CLOSED'));}
    pending.clear(); socket.close();
  }};
}

async function recoverCity(config, dependencies) {
  const {inspectPort, createPageTarget, listTargets, command, fetchJson} = dependencies;
  const evidence = {status: 'OWNER_AUTH_REQUIRED', logout_notice_ack_count: 0, submit_count: 0, password_typed: false, owned_target_closed: false};
  let id, client;
  try {
    if ((await inspectPort(config)).state !== 'MATCH') throw new Error('CDP_PORT_RUNTIME_MISMATCH');
    const version = await fetchJson(`http://127.0.0.1:${config.cdpPort}/json/version`);
    id = await createPageTarget(config, 'about:blank');
    const target = (await listTargets(config.cdpPort)).find(t => t.id === id);
    if (!target) throw new Error('AUTH_TARGET_MISSING');
    client = (dependencies.connect || connect)(target.webSocketDebuggerUrl, evidence);
    await client.call('Page.enable');
    await client.call('Network.enable');
    await client.call('Page.navigate', {url: 'https://newmanager.cityheaven.net/C2GirlList.php?shopdir=s_apronlady'});
    const deadline = Date.now() + 25000;
    for (let attempt = 0; attempt < 24 && Date.now() < deadline; attempt++) {
      await (dependencies.wait || delay)(500);
      if (evidence.blocked) break;
      let result;
      try {
        result = await client.call('Runtime.evaluate', {expression: '({origin:location.origin,ready:document.readyState,path:location.pathname,manager:!!document.querySelector("form[action*=C2GirlList],a[href*=member_id],a[href*=C2TokeiGirl]")})', returnByValue: true});
      } catch { if (evidence.blocked) break; else continue; }
      const state = result?.result?.value;
      if (state?.origin !== 'https://newmanager.cityheaven.net' || state?.ready !== 'complete') continue;
      if (evidence.submit_count) {
        // Login can succeed while its landing page lacks the girl's-list menu.
        // Prove the session with the same protected read the collector uses.
        const proof = await client.call('Runtime.evaluate', {expression: `(async()=>{const r=await fetch('/C2GirlList.php?shopdir=s_apronlady',{method:'GET',credentials:'include',cache:'no-store',redirect:'follow'});const u=new URL(r.url);if([401,403,429].includes(r.status))return {state:'ACCESS_RESTRICTED'};if(r.status!==200||u.origin!==location.origin)return {state:'SOURCE_UNAVAILABLE'};if(u.pathname==='/C1Login.php')return {state:'AUTH_REQUIRED'};const d=new DOMParser().parseFromString(await r.text(),'text/html');return {state:u.pathname==='/C2GirlList.php'&&!!d.querySelector('form[action*=C2GirlList],a[href*=member_id],[onclick*=member_id]')?'AUTHENTICATED':'AUTH_REQUIRED'};})()`,returnByValue:true,awaitPromise:true});
        evidence.auth_proof_request_count = (evidence.auth_proof_request_count || 0) + 1;
        const proofState = proof?.result?.value?.state;
        if (proofState === 'AUTHENTICATED') {evidence.status = 'AUTHENTICATED'; break;}
        if (proofState === 'ACCESS_RESTRICTED' || proofState === 'SOURCE_UNAVAILABLE') {evidence.blocked = proofState; break;}
        if (evidence.auth_proof_request_count >= 3) break;
        await (dependencies.wait || delay)(1000);
        continue;
      }
      if (state.path !== '/C1Login.php' && state.manager) {evidence.status = 'AUTHENTICATED'; break;}
      const checked = await client.call('Runtime.evaluate', {expression: `(${pageSource})("city",false)`, returnByValue: true});
      const code = checked?.result?.value?.code;
      if (code === 'SAVED_FIELDS_EMPTY' || code === 'NOT_LOGIN_PAGE') continue;
      if (code !== 'READY') {evidence.blocked = code || 'LOGIN_STATE_UNKNOWN'; break;}
      // Budget consumed before send, including uncertain navigation outcomes.
      evidence.submit_count = 1;
      try {
        const sent = await client.call('Runtime.evaluate', {expression: `(${pageSource})("city",true)`, returnByValue: true, userGesture: true});
        if (sent?.result?.value?.submitted === false) {evidence.submit_count = 0; evidence.blocked = sent.result.value.code; break;}
      } catch { /* Observe the outcome; never submit again. */ }
    }
    if (evidence.blocked) evidence.status = evidence.blocked;
    // The collector performs a fresh authenticated source read after this proof.
    if (id) {
      client.close(); client = null;
      const closed = await command(version.webSocketDebuggerUrl, 'Target.closeTarget', {targetId: id});
      evidence.owned_target_closed = closed?.success === true;
      id = null;
    }
  } catch (error) {
    evidence.status = ['CDP_PORT_RUNTIME_MISMATCH','AUTH_TARGET_MISSING','AUTH_COMMAND_TIMEOUT','AUTH_COMMAND_FAILED'].includes(error.message) ? error.message : 'OWNER_AUTH_REQUIRED';
  } finally {
    client?.close();
    if (id) {
      try {
        const version = await fetchJson(`http://127.0.0.1:${config.cdpPort}/json/version`);
        evidence.owned_target_closed = (await command(version.webSocketDebuggerUrl, 'Target.closeTarget', {targetId: id}))?.success === true;
      } catch {evidence.owned_target_closed = false;}
    }
  }
  return evidence;
}
module.exports = {recoverCity, logoutMessages, connect};
