from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


class CityHeavenReservationRuntimeTests(unittest.TestCase):
    def test_official_cms_auth_bootstrap_is_single_open_bounded_and_fail_closed(self):
        root = Path(__file__).resolve().parents[1]
        module = root / "src" / "windows_execution_foundation" / "cityheaven_reservation_runtime_reader.js"
        script = r"""
const assert = require('node:assert/strict');
const runtime = require(process.argv[1]);
const config = runtime.runtimeConfig({browser:'CHROME', cdp_port:9227,
  chrome_executable:'C:\\Chrome\\chrome.exe', profile_path:'C:\\Profiles\\Reservation',
  base_reader_path:process.argv[1], startup_lock_path:'C:\\Locks\\reservation.lock'});
const cms = {id:'cms', url:'https://newmanager.cityheaven.net/C1Main.php',
  webSocketDebuggerUrl:'ws://cms'};
const pro = {id:'pro', url:'https://pro-manager.cityheaven.net/', webSocketDebuggerUrl:'ws://pro'};
const expectCode = async (fn, code) => assert.rejects(fn, error => error.runtimeCode === code);
(async () => {
  let cmsInspects = 0, opens = 0, creates = 0, credentialAutomation = 0;
  let result = await runtime.bootstrapOfficialAuth(config, null, {
    listTargets:async()=>[pro], inspectProTarget:async()=>({authenticated:true,ready:true}),
    inspectCmsTarget:async()=>{cmsInspects++; return {};},
    openCmsLink:async()=>{opens++; return {};}, createTarget:async()=>{creates++;},
  });
  assert.equal(result.existingProManagerReused, true);
  assert.equal(result.cmsEntryUsed, false);
  assert.deepEqual({cmsInspects, opens, creates}, {cmsInspects:0, opens:0, creates:0});

  result = await runtime.bootstrapOfficialAuth(config, null, {
    listTargets:async()=>[pro, {...pro, id:'pro-home', url:'https://pro-manager.cityheaven.net/'}],
    inspectProTarget:async()=>({authenticated:true,ready:true}),
    inspectCmsTarget:async()=>{throw new Error('CMS_MUST_NOT_BE_INSPECTED');},
  });
  assert.equal(result.existingProManagerReused, true);
  assert.equal(result.target.id, 'pro');

  let lists = 0, userGestures = 0;
  result = await runtime.bootstrapOfficialAuth(config, null, {
    listTargets:async()=> ++lists === 1 ? [cms] : [cms, pro],
    inspectProTarget:async()=>({authenticated:true,ready:true}),
    inspectCmsTarget:async()=>({authenticated:true, authRequired:false, linkCount:1,
      officialHref:'https://newmanager.cityheaven.net/ReservationLogin?shopdir=s_apronlady'}),
    command:async (_socket, method, params) => {
      assert.equal(method, 'Runtime.evaluate');
      if (params.userGesture === true) userGestures++;
      return {result:{value:{opened:true, linkCount:1}}};
    },
    wait:async()=>{},
  });
  assert.equal(result.existingProManagerReused, false);
  assert.equal(result.cmsEntryUsed, true);
  assert.equal(result.proManagerState.ready, true);
  assert.equal(userGestures, 1);

  await expectCode(() => runtime.bootstrapOfficialAuth(config, null, {
    listTargets:async()=>[cms], inspectCmsTarget:async()=>({authenticated:false, authRequired:true, linkCount:0}),
  }), 'AUTH_REQUIRED_CMS');

  for (const linkCount of [0, 2]) {
    opens = 0;
    await expectCode(() => runtime.bootstrapOfficialAuth(config, null, {
      listTargets:async()=>[cms],
      inspectCmsTarget:async()=>({authenticated:true, authRequired:false, linkCount}),
      openCmsLink:async()=>{opens++; return {opened:true, linkCount};},
    }), 'CITY_AUTH_TRANSFER_FAILED');
    assert.equal(opens, 0);
  }

  opens = 0; lists = 0;
  await expectCode(() => runtime.bootstrapOfficialAuth(config, null, {
    listTargets:async()=> ++lists === 1 ? [cms] : [cms, pro],
    inspectCmsTarget:async()=>({authenticated:true, authRequired:false, linkCount:1}),
    openCmsLink:async()=>{opens++; return {opened:true, linkCount:1};},
    inspectProTarget:async()=>({authenticated:false, transferFailed:true}), wait:async()=>{},
  }), 'CITY_AUTH_TRANSFER_FAILED');
  assert.equal(opens, 1);

  opens = 0; lists = 0;
  await expectCode(() => runtime.bootstrapOfficialAuth(config, null, {
    listTargets:async()=>{lists++; return [cms];},
    inspectCmsTarget:async()=>({authenticated:true, authRequired:false, linkCount:1}),
    openCmsLink:async()=>{opens++; return {opened:true, linkCount:1};}, wait:async()=>{},
  }), 'CITY_AUTH_TRANSFER_FAILED');
  assert.equal(opens, 1);
  assert.equal(lists, 17);

  opens = 0; lists = 0;
  await expectCode(() => runtime.bootstrapOfficialAuth(config, null, {
    listTargets:async()=> ++lists === 1 ? [cms] : [cms, pro, {...pro, id:'pro-duplicate'}],
    inspectCmsTarget:async()=>({authenticated:true, authRequired:false, linkCount:1}),
    openCmsLink:async()=>{opens++; return {opened:true, linkCount:1};},
    inspectProTarget:async()=>({authenticated:true,ready:true}), wait:async()=>{},
  }), 'CITY_AUTH_TRANSFER_FAILED');
  assert.equal(opens, 1);

  let currentTargets = [cms];
  opens = 0;
  const reusableDependencies = {
    listTargets:async()=>currentTargets,
    inspectCmsTarget:async()=>({authenticated:true, authRequired:false, linkCount:1}),
    openCmsLink:async()=>{opens++; currentTargets = [cms, pro]; return {opened:true, linkCount:1};},
    inspectProTarget:async()=>({authenticated:true,ready:true}), wait:async()=>{},
  };
  await runtime.bootstrapOfficialAuth(config, null, reusableDependencies);
  await runtime.bootstrapOfficialAuth(config, null, reusableDependencies);
  assert.equal(opens, 1);

  const savedLocation = global.location, savedDocument = global.document;
  const savedWindow = global.window, savedFetch = global.fetch, savedDOMParser = global.DOMParser;
  global.location = {origin:'https://newmanager.cityheaven.net',
    href:'https://newmanager.cityheaven.net/C1Main.php', pathname:'/C1Main.php', search:''};
  global.DOMParser = class { parseFromString() { return {querySelector:()=>null}; }};
  global.fetch = async()=>({ok:true,status:200,url:'https://newmanager.cityheaven.net/C1Main.php',
    text:async()=>'<html><title>CMS</title></html>'});
  const anchor = (href, target='pro-manager') => ({href:new URL(href, global.location.href).href,
    getAttribute:name => name === 'href' ? href : name === 'target' ? target : null});
  global.document = {querySelector:()=>null, querySelectorAll:()=>[
    anchor('/ReservationLogin?shopdir=wrong'),
    anchor('https://evil.invalid/ReservationLogin?shopdir=s_apronlady'),
    anchor('/ReservationLogin?shopdir=s_apronlady', 'wrong-target'),
    anchor('/ReservationLogin?shopdir=s_apronlady&extra=1'),
  ]};
  assert.equal((await runtime.cmsReservationLoginStateInPage('s_apronlady', false)).linkCount, 0);
  global.document = {querySelector:()=>null, querySelectorAll:()=>[
    anchor('/ReservationLogin?shopdir=s_apronlady'),
    anchor('/ReservationLogin/?chartflg=1&shopdir=s_apronlady'),
    anchor('/ReservationLogin?shopdir=s_apronlady'),
    anchor('/ReservationLogin/?chatflg=1&shopdir=s_apronlady'),
    anchor('/ReservationLogin?shopdir=s_apronlady'),
  ]};
  const duplicateCmsState = await runtime.cmsReservationLoginStateInPage('s_apronlady', false);
  assert.equal(duplicateCmsState.authenticated, true);
  assert.equal(duplicateCmsState.linkCount, 1);
  let openedHref = null, openedTarget = null, openCount = 0;
  global.window = {open:(href, target)=>{openCount++; openedHref=href; openedTarget=target; return {};}};
  const cmsOpen = await runtime.cmsReservationLoginStateInPage('s_apronlady', true);
  assert.equal(cmsOpen.opened, true);
  assert.equal(cmsOpen.linkCount, 1);
  assert.equal(openCount, 1);
  assert.equal(openedHref, 'https://newmanager.cityheaven.net/ReservationLogin?shopdir=s_apronlady');
  assert.equal(openedTarget, 'pro-manager');

  global.fetch = async()=>({ok:true,status:200,url:'https://newmanager.cityheaven.net/C1Login.php',
    text:async()=>'<html><input type="password"></html>'});
  global.DOMParser = class { parseFromString() {
    return {querySelector:selector=>selector.includes('password') ? {} : null};
  }};
  const expiredCms = await runtime.cmsReservationLoginStateInPage('s_apronlady', true);
  assert.equal(expiredCms.authenticated, false);
  assert.equal(expiredCms.authRequired, true);
  assert.equal(expiredCms.opened, false);
  assert.equal(openCount, 1);

  global.location = {origin:'https://pro-manager.cityheaven.net', pathname:'/', search:''};
  global.document = {title:'ホーム', body:{innerText:''}, querySelector:()=>null};
  global.DOMParser = class { parseFromString() {
    return {title:'受付台帳', body:{innerText:''}, querySelector:()=>null};
  }};
  global.fetch = async()=>({ok:true,status:200,url:'https://pro-manager.cityheaven.net/reservation/timechart',
    text:async()=>'<html><title>受付台帳</title></html>'});
  let proState = await runtime.proManagerStateInPage();
  assert.equal(proState.authenticated, true);
  assert.equal(proState.ready, true);
  global.fetch = async()=>({ok:false,status:401,url:'https://pro-manager.cityheaven.net/reservation/timechart',
    text:async()=>'<html><title>401 Unauthorized</title></html>'});
  proState = await runtime.proManagerStateInPage();
  assert.equal(proState.authenticated, false);
  assert.equal(proState.transferFailed, true);
  global.location = savedLocation; global.document = savedDocument; global.window = savedWindow;
  global.fetch = savedFetch; global.DOMParser = savedDOMParser;
  const source = require('node:fs').readFileSync(process.argv[1], 'utf8');
  for (const forbidden of ['loginId', 'txt_password', '.submit()', 'requestSubmit()']) {
    if (source.includes(forbidden)) credentialAutomation++;
  }
  process.stdout.write(JSON.stringify({status:'PASS', officialOpenCount:1,
    credentialAutomationCount:credentialAutomation}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            ["node", "-e", script, str(module)], capture_output=True, text=True,
            encoding="utf-8", check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "status": "PASS", "officialOpenCount": 1, "credentialAutomationCount": 0,
        })

    def test_top_level_failure_receipt_is_allowlisted_sanitized_and_non_overwriting(self):
        root = Path(__file__).resolve().parents[1]
        runner = root / "src" / "windows_execution_foundation" / "cityheaven_reservation_once.js"
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const childProcess = require('node:child_process');
const runner = process.argv[1];
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'city-top-level-receipt-'));
const makeManifest = (name, readerPath) => {
  const receiptPath = path.join(tmp, `${name}.receipt.json`);
  const manifestPath = path.join(tmp, `${name}.manifest.json`);
  fs.writeFileSync(manifestPath, JSON.stringify({reservation_key:`key-${name}`,
    request_hash:'a'.repeat(64), reservation_at:'2026-09-27T04:00:00Z', cast:'CAST-A',
    course:'COURSE-A', receipt_path:receiptPath, reader_path:readerPath}), 'utf8');
  return {manifestPath, receiptPath};
};
const run = manifestPath => childProcess.spawnSync(process.execPath, [runner, manifestPath],
  {encoding:'utf8'});
const runtimeReader = path.join(tmp, 'runtime-reader.js');
fs.writeFileSync(runtimeReader, `exports.BrowserReader = class { async ensure() {
  const error = new Error('raw secret URL https://example.invalid/?token=secret');
  error.runtimeCode = 'CDP_TIMEOUT'; error.stack = 'PRIVATE STACK'; throw error;
} };`, 'utf8');
let item = makeManifest('runtime-code', runtimeReader);
assert.equal(run(item.manifestPath).status, 30);
let receipt = JSON.parse(fs.readFileSync(item.receiptPath, 'utf8'));
assert.equal(receipt.reason_code, 'CDP_TIMEOUT');
assert.equal(receipt.candidate_count, null);

const messageReader = path.join(tmp, 'message-reader.js');
fs.writeFileSync(messageReader, `exports.BrowserReader = class { async ensure() {
  throw new Error('CDP_CONNECTION_FAILED');
} };`, 'utf8');
item = makeManifest('message-code', messageReader);
assert.equal(run(item.manifestPath).status, 30);
receipt = JSON.parse(fs.readFileSync(item.receiptPath, 'utf8'));
assert.equal(receipt.reason_code, 'CDP_CONNECTION_FAILED');

const unknownReader = path.join(tmp, 'unknown-reader.js');
fs.writeFileSync(unknownReader, `exports.BrowserReader = class { async ensure() {
  throw new Error('customer private-rsv-id https://example.invalid/?cookie=secret');
} };`, 'utf8');
item = makeManifest('unknown', unknownReader);
assert.equal(run(item.manifestPath).status, 30);
const serialized = fs.readFileSync(item.receiptPath, 'utf8');
receipt = JSON.parse(serialized);
assert.equal(receipt.reason_code, 'RUNTIME_UNCLASSIFIED');
for (const forbidden of ['private-rsv-id', 'example.invalid', 'cookie=secret', 'stack']) {
  assert.ok(!serialized.toLowerCase().includes(forbidden));
}
assert.equal(receipt.send_enabled, false);
assert.equal(receipt.customer_send_count, 0);
assert.equal(receipt.reservation_mutation_count, 0);
assert.equal(receipt.external_change_count, 0);

item = makeManifest('existing', unknownReader);
fs.writeFileSync(item.receiptPath, '{"preserved":true}\n', 'utf8');
assert.equal(run(item.manifestPath).status, 30);
assert.deepEqual(JSON.parse(fs.readFileSync(item.receiptPath, 'utf8')), {preserved:true});

const invalidManifest = path.join(tmp, 'invalid.manifest.json');
const unsafeReceipt = path.join(tmp, 'unsafe.receipt.json');
fs.writeFileSync(invalidManifest, JSON.stringify({receipt_path:unsafeReceipt}), 'utf8');
assert.equal(run(invalidManifest).status, 30);
assert.equal(fs.existsSync(unsafeReceipt), false);
assert.equal(run(path.join(tmp, 'missing.manifest.json')).status, 30);

const successReader = path.join(tmp, 'success-reader.js');
fs.writeFileSync(successReader, `exports.BrowserReader = class {
  async ensure() {} async city() { return []; }
};`, 'utf8');
item = makeManifest('normal', successReader);
assert.equal(run(item.manifestPath).status, 20);
receipt = JSON.parse(fs.readFileSync(item.receiptPath, 'utf8'));
assert.equal(receipt.reason_code, 'CANDIDATE_NOT_FOUND');
process.stdout.write(JSON.stringify({status:'PASS'}));
"""
        completed = subprocess.run(
            ["node", "-e", script, str(runner)], capture_output=True, text=True,
            encoding="utf-8", check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"status": "PASS"})

    def test_dedicated_runtime_lifecycle_and_fail_closed_contract(self):
        root = Path(__file__).resolve().parents[1]
        module = root / "src" / "windows_execution_foundation" / "cityheaven_reservation_runtime_reader.js"
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const runtime = require(process.argv[1]);
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'city-runtime-test-'));
const raw = {browser:'CHROME', cdp_port:9227, chrome_executable:'C:\\Chrome\\chrome.exe',
  profile_path:path.join(tmp,'ReservationRuntimeChromeProfile'), base_reader_path:process.argv[1],
  startup_lock_path:path.join(tmp,'runtime.lock')};
(async () => {
  let launches = 0;
  const reused = runtime.createRuntimeEnsurer(raw, {inspect:async()=>({state:'MATCH'}),
    launch:async()=>{ launches++; return {profileCreated:false}; }});
  assert.deepEqual(await reused.ensure(), {reused:true, profileCreated:false});
  assert.equal(launches, 0);

  let inspections = 0;
  const started = runtime.createRuntimeEnsurer(raw, {
    inspect:async()=>({state: inspections++ === 0 ? 'ABSENT' : 'MATCH'}), wait:async()=>{},
    launch:async()=>{ launches++; return {profileCreated:true}; }});
  assert.deepEqual(await started.ensure(), {reused:false, profileCreated:true});
  assert.equal(launches, 1);

  const wrong = runtime.createRuntimeEnsurer(raw, {inspect:async()=>({state:'MISMATCH'}),
    launch:async()=>{ launches++; return {profileCreated:false}; }});
  await assert.rejects(wrong.ensure(), /CDP_PORT_RUNTIME_MISMATCH/);
  assert.equal(launches, 1);

  assert.equal(runtime.classifyReadiness({authenticated:false,ready:false}, true,
    {reused:false,profileCreated:true}).auth_state, 'AUTH_REQUIRED');
  assert.equal(runtime.classifyReadiness({authenticated:true,ready:true}, false,
    {reused:true,profileCreated:false}).reservation_list_ready, true);
  assert.throws(()=>runtime.runtimeConfig({...raw, cdp_port:9225}), /CHROME_RUNTIME_CONFIG_INVALID/);
  assert.deepEqual(await runtime.inspectPort(runtime.runtimeConfig(raw), {
    fetchJson:async()=>{ throw new TypeError('not-http'); }, tcpListening:async()=>true,
  }), {state:'MISMATCH'});

  const config = runtime.runtimeConfig(raw);
  const version = {Browser:'Chrome/153.0.8010.54', webSocketDebuggerUrl:'ws://127.0.0.1/devtools/browser/id'};
  assert.equal(runtime.bodyContainsExactProfile(`"${config.profile}"`, config.profile), true);
  assert.equal(runtime.bodyContainsExactProfile(`Profile Path ${config.profile} `, config.profile), true);
  assert.equal(runtime.bodyContainsExactProfile(`${config.profile}\\Default`, config.profile), true);
  assert.equal(runtime.bodyContainsExactProfile(`X${config.profile}`, config.profile), false);
  assert.equal(runtime.bodyContainsExactProfile(`${config.profile}X`, config.profile), false);
  assert.equal(runtime.bodyContainsExactProfile('C:\\unrelated\\profile', config.profile), false);
  const inspect = command => runtime.inspectPort(config, {fetchJson:async()=>version, command});
  assert.deepEqual(await inspect(async()=>({arguments:['chrome.exe','--remote-debugging-port=9227',
    `--user-data-dir=${config.profile}`]})), {state:'MATCH'});
  assert.deepEqual(await inspect(async()=>({arguments:['chrome.exe','--remote-debugging-port=9228',
    `--user-data-dir=${config.profile}`]})), {state:'MISMATCH'});
  assert.deepEqual(await inspect(async()=>({arguments:['chrome.exe','--remote-debugging-port=9227',
    `--user-data-dir=${config.profile}-wrong`]})), {state:'MISMATCH'});

  const noAutomation = Object.assign(new Error('CDP_COMMAND_FAILED'), {cdpCode:-32000,
    cdpMessage:'Command line not returned because --enable-automation not set.'});
  const fallback = ({body=`Profile Path ${config.profile}\\Default`, createError=false,
    readError=false, closeError=false}={}) => {
    const calls = [];
    let osEnumerationCalls = 0;
    const result = runtime.inspectPort(config, {
      fetchJson:async url => url.endsWith('/json/version') ? version :
        [{id:'version-target', webSocketDebuggerUrl:'ws://127.0.0.1/devtools/page/version-target'}],
      command:async (_target, method) => {
        calls.push(method);
        if (method === 'Browser.getBrowserCommandLine') throw noAutomation;
        if (method === 'Target.createTarget') {
          if (createError) throw new Error('create failed');
          return {targetId:'version-target'};
        }
        if (method === 'Runtime.evaluate') {
          if (readError) throw new Error('read failed');
          return {result:{value:body}};
        }
        if (method === 'Target.closeTarget') {
          if (closeError) throw new Error('close failed');
          return {success:true};
        }
        throw new Error(`unexpected method ${method}`);
      },
      listChromeProcessCommandLines:async()=>{ osEnumerationCalls++; return []; },
    });
    return {result, calls, osEnumerationCalls};
  };
  let probe = fallback();
  assert.deepEqual(await probe.result, {state:'MATCH'});
  assert.equal(probe.osEnumerationCalls, 0);
  assert.deepEqual(probe.calls, ['Browser.getBrowserCommandLine','Target.createTarget',
    'Runtime.evaluate','Target.closeTarget']);
  probe = fallback({body:`Profile Path "${config.profile}"`});
  assert.deepEqual(await probe.result, {state:'MATCH'});
  probe = fallback({body:`Profile Path ${config.profile}-wrong\\Default`});
  assert.deepEqual(await probe.result, {state:'MISMATCH'});
  probe = fallback({createError:true});
  assert.deepEqual(await probe.result, {state:'MISMATCH'});
  assert.ok(!probe.calls.includes('Target.closeTarget'));
  probe = fallback({readError:true});
  assert.deepEqual(await probe.result, {state:'MISMATCH'});
  assert.ok(probe.calls.includes('Target.closeTarget'));
  probe = fallback({closeError:true});
  assert.deepEqual(await probe.result, {state:'MISMATCH'});
  assert.ok(probe.calls.includes('Target.closeTarget'));
  assert.deepEqual(await runtime.inspectPort(config, {fetchJson:async()=>version,
    command:async()=>{throw Object.assign(new Error('other'), {cdpCode:-32001,
      cdpMessage:'Unrelated failure'});}, listChromeProcessCommandLines:async()=>{throw new Error('must not call');}}), {state:'MISMATCH'});
  assert.deepEqual(await runtime.inspectPort(config, {fetchJson:async()=>version,
    command:async()=>{throw Object.assign(new Error('other'), {cdpCode:-32000,
      cdpMessage:'Prefix: Command line not returned because --enable-automation not set.'});},
    listChromeProcessCommandLines:async()=>{throw new Error('must not call');}}), {state:'MISMATCH'});

  let launchArgs;
  await runtime.launchChrome(config, {exists:()=>true, spawn:(_executable,args)=>{
    launchArgs = args; const listeners = {}; return {once:(name,fn)=>{listeners[name]=fn;
      if(name==='spawn') queueMicrotask(fn);}, unref:()=>{}};
  }});
  assert.ok(!launchArgs.includes('--enable-automation'));
  process.stdout.write(JSON.stringify({status:'PASS', launches, unrelated_process_actions:0}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            ["node", "-e", script, str(module)], capture_output=True, text=True,
            encoding="utf-8", check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "status": "PASS", "launches": 1, "unrelated_process_actions": 0,
        })

    def test_production_config_selects_chrome_runtime_and_preserves_dry_run(self):
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root / "local" / "config.json").read_text(encoding="utf-8"))
        city = config["cityheaven_reservation"]
        self.assertEqual(city["cdp_port"], 9227)
        self.assertTrue(city["chrome_executable"].lower().endswith("chrome.exe"))
        self.assertIn("ReservationRuntimeChromeProfile", city["dedicated_profile_path"])
        self.assertNotIn("Opera", json.dumps(city, ensure_ascii=False))
        source = (root / "src" / "windows_execution_foundation" / "cityheaven_reservation.py").read_text(encoding="utf-8")
        self.assertIn('"start_url": "https://newmanager.cityheaven.net/"', source)
        self.assertNotIn('"start_url": "https://pro-manager.cityheaven.net', source)
        runner = (root / "src" / "windows_execution_foundation" / "cityheaven_reservation_once.js").read_text(encoding="utf-8")
        self.assertIn("send_enabled: false", runner)
        self.assertNotIn(".save(", runner)

    def test_daily_readiness_reuses_identity_and_never_opens_auth_handoff(self):
        root = Path(__file__).resolve().parents[1]
        module = root / "src" / "windows_execution_foundation" / "cityheaven_reservation_runtime_reader.js"
        script = r"""
const assert = require('node:assert/strict');
const runtime = require(process.argv[1]);
const raw = {browser:'CHROME', cdp_port:9227, chrome_executable:'C:\\Chrome\\chrome.exe',
  profile_path:'C:\\Profiles\\ReservationRuntimeChromeProfile', base_reader_path:process.argv[1],
  startup_lock_path:'C:\\Locks\\reservation.lock'};
const cms = {id:'cms', url:'https://newmanager.cityheaven.net/C2GirlList.php', webSocketDebuggerUrl:'ws://cms'};
(async () => {
  let launches = 0, opens = 0, targetLists = 0, cmsInspects = 0;
  const dependencies = {
    exists:()=>true, inspect:async()=>({state:'MATCH'}),
    launch:async()=>{launches++; return {profileCreated:false};},
    listTargets:async()=>{targetLists++; return [cms];},
    command:async()=>({result:{value:{origin:'https://newmanager.cityheaven.net',ready:'complete'}}}),
    inspectCmsTarget:async()=>{cmsInspects++; return {authenticated:false,authRequired:false,linkCount:0};},
    openCmsLink:async()=>{opens++;},
  };
  const result = await runtime.dailyReadiness(raw, dependencies);
  assert.equal(result.runtime_identity, 'MATCH');
  assert.equal(result.profile_identity, 'MATCH');
  assert.equal(result.auth_state, 'DEFERRED_TO_DAILY_READER');
  assert.equal(result.daily_manager_ready, true);
  assert.equal(result.existing_process_reused, true);
  assert.equal(result.additional_browser_launch_count, 0);
  assert.deepEqual({launches,opens,targetLists,cmsInspects}, {launches:0,opens:0,targetLists:1,cmsInspects:0});
  await assert.rejects(runtime.dailyReadiness(raw, {...dependencies, exists:()=>false}),
    error => error.runtimeCode === 'CHROME_RUNTIME_UNAVAILABLE');
  process.stdout.write(JSON.stringify({status:'PASS',launches,opens}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(
            ["node", "-e", script, str(module)], capture_output=True, text=True,
            encoding="utf-8", check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"status": "PASS", "launches": 0, "opens": 0})


if __name__ == "__main__":
    unittest.main()
