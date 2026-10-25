from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from src.windows_execution_foundation import cityheaven_daily_metrics as metrics


ROOT = Path(__file__).resolve().parents[1]


class DailyOperaRuntimeTests(unittest.TestCase):
    def test_config_and_lifecycle_contract(self):
        module = ROOT / "src" / "windows_execution_foundation" / "cityheaven_daily_opera_runtime.js"
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const runtime = require(process.argv[1]);
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'daily-opera-test-'));
const raw = {browser:'OPERA', cdp_port:9236, opera_executable:'C:\\Opera\\opera.exe',
  dedicated_profile_path:path.join(tmp,'DailyMetricsOperaProfile'),
  startup_lock_path:path.join(tmp,'runtime.lock'), start_url:'https://newmanager.cityheaven.net/'};
(async () => {
  let launches = 0;
  const reused = runtime.createRuntimeEnsurer(raw, {inspect:async()=>({state:'MATCH'}),
    launch:async()=>{ launches++; return {profileCreated:false}; }});
  assert.deepEqual(await reused.ensure(), {reused:true, profileCreated:false});
  assert.equal(launches, 0);
  let inspections = 0;
  const started = runtime.createRuntimeEnsurer(raw, {inspect:async()=>({state:inspections++ ? 'MATCH':'ABSENT'}),
    wait:async()=>{}, launch:async()=>{ launches++; return {profileCreated:true}; }});
  assert.deepEqual(await started.ensure(), {reused:false, profileCreated:true});
  assert.equal(launches, 1);
  let delayedInspections = 0;
  let delayedWaits = 0;
  const delayed = runtime.createRuntimeEnsurer(raw, {
    inspect:async()=>({state:['ABSENT','ABSENT','MATCH'][delayedInspections++]}),
    wait:async milliseconds=>{ assert.equal(milliseconds,2500); delayedWaits++; },
    launch:async()=>({profileCreated:false})});
  assert.deepEqual(await delayed.ensure(), {reused:false,profileCreated:false});
  assert.equal(delayedInspections,3);
  assert.equal(delayedWaits,2);
  let startingInspections = 0;
  let startingWaits = 0;
  const starting = runtime.createRuntimeEnsurer(raw, {
    inspect:async()=>({state:['ABSENT','STARTING','MATCH'][startingInspections++]}),
    wait:async milliseconds=>{ assert.equal(milliseconds,2500); startingWaits++; },
    launch:async()=>({profileCreated:false})});
  assert.deepEqual(await starting.ensure(), {reused:false,profileCreated:false});
  assert.equal(startingInspections,3);
  assert.equal(startingWaits,2);
  let unavailableInspections = 0;
  let unavailableWaits = 0;
  const unavailable = runtime.createRuntimeEnsurer(raw, {
    inspect:async()=>({state:['ABSENT','STARTING','ABSENT'][unavailableInspections++]}),
    wait:async milliseconds=>{ assert.equal(milliseconds,2500); unavailableWaits++; },
    launch:async()=>({profileCreated:false})});
  await assert.rejects(unavailable.ensure(), /BROWSER_CDP_UNAVAILABLE/);
  assert.equal(unavailableInspections,3);
  assert.equal(unavailableWaits,2);
  let mismatchInspections = 0;
  let mismatchWaits = 0;
  const postSpawnMismatch = runtime.createRuntimeEnsurer(raw, {
    inspect:async()=>({state:mismatchInspections++ ? 'MISMATCH':'ABSENT'}),
    wait:async milliseconds=>{ assert.equal(milliseconds,2500); mismatchWaits++; },
    launch:async()=>({profileCreated:false})});
  await assert.rejects(postSpawnMismatch.ensure(), /CDP_PORT_RUNTIME_MISMATCH/);
  assert.equal(mismatchInspections,2);
  assert.equal(mismatchWaits,1);
  const wrong = runtime.createRuntimeEnsurer(raw, {inspect:async()=>({state:'MISMATCH'}),
    launch:async()=>{ launches++; return {profileCreated:false}; }});
  await assert.rejects(wrong.ensure(), /CDP_PORT_RUNTIME_MISMATCH/);
  assert.equal(launches, 1);
  assert.throws(()=>runtime.runtimeConfig({...raw, cdp_port:9225}), /OPERA_RUNTIME_CONFIG_INVALID/);
  const occupied = await runtime.inspectPort(runtime.runtimeConfig(raw), {
    fetchJson:async()=>{ throw new TypeError('not-cdp'); }, tcpListening:async()=>true});
  assert.deepEqual(occupied, {state:'MISMATCH'});
  const matching = await runtime.inspectPort(runtime.runtimeConfig(raw), {
    fetchJson:async()=>({webSocketDebuggerUrl:'ws://test','User-Agent':'Mozilla/5.0 OPR/136.0'}),
    command:async()=>({arguments:['opera.exe','--remote-debugging-port=9236',
      '--user-data-dir='+raw.dedicated_profile_path]})});
  assert.deepEqual(matching, {state:'MATCH'});
  const startingPort = await runtime.inspectPort(runtime.runtimeConfig(raw), {
    fetchJson:async()=>({webSocketDebuggerUrl:'ws://test','User-Agent':'Mozilla/5.0 OPR/136.0'}),
    command:async()=>{ throw new Error('secret transient transport detail'); }});
  assert.deepEqual(startingPort, {state:'STARTING'});
  for (const arguments_ of [
    ['opera.exe','--remote-debugging-port=9237','--user-data-dir='+raw.dedicated_profile_path],
    ['opera.exe','--remote-debugging-port=9236','--user-data-dir='+path.join(tmp,'OtherProfile')],
    ['opera.exe','--remote-debugging-port=9236'],
  ]) {
    const mismatch = await runtime.inspectPort(runtime.runtimeConfig(raw), {
      fetchJson:async()=>({webSocketDebuggerUrl:'ws://test','User-Agent':'Mozilla/5.0 OPR/136.0'}),
      command:async()=>({arguments:arguments_})});
    assert.deepEqual(mismatch, {state:'MISMATCH'});
  }
  const wrongUa = await runtime.inspectPort(runtime.runtimeConfig(raw), {
    fetchJson:async()=>({webSocketDebuggerUrl:'ws://test','User-Agent':'Mozilla/5.0 Chrome/136.0'}),
    command:async()=>{ throw new Error('must not be called'); }});
  assert.deepEqual(wrongUa, {state:'MISMATCH'});
  let launchArgs;
  await runtime.launchOpera(runtime.runtimeConfig(raw), {exists:()=>true,
    spawn:(executable,args)=>{ launchArgs=args; const child=new EventEmitter(); child.unref=()=>{};
      queueMicrotask(()=>child.emit('spawn')); return child; }});
  assert.ok(launchArgs.includes('--enable-automation'));
  process.stdout.write(JSON.stringify({status:'PASS',launches,normal_opera_actions:0,
    browser_connector_actions:0,reservation_runtime_actions:0,cityheaven_writes:0}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run(["node", "-e", script, str(module)], capture_output=True,
                                   text=True, encoding="utf-8", check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result, {"status": "PASS", "launches": 1, "normal_opera_actions": 0,
                                  "browser_connector_actions": 0, "reservation_runtime_actions": 0,
                                  "cityheaven_writes": 0})

    def test_daily_config_preserves_opera_rollback_fields_but_routes_to_reservation_chrome(self):
        config = json.loads((ROOT / "local" / "config.json").read_text(encoding="utf-8"))
        daily, reservation = config["cityheaven_daily"], config["cityheaven_reservation"]
        self.assertEqual(daily["browser"], "RESERVATION_CHROME_REUSE")
        self.assertEqual(daily["cdp_port"], 9236)
        self.assertIn("DailyMetricsOperaProfile", daily["dedicated_profile_path"])
        self.assertNotEqual(daily["cdp_port"], reservation["cdp_port"])
        self.assertNotEqual(daily["dedicated_profile_path"], reservation["dedicated_profile_path"])
        self.assertNotIn("extension", json.dumps(daily).lower())

    def test_reader_and_collector_inject_daily_port_never_9225(self):
        reader = ROOT / "src" / "windows_execution_foundation" / "cityheaven_daily_reader.js"
        script = "const r=require(process.argv[1]);process.stdout.write(String(r.configuredPort(['--config',process.argv[2]],{})))"
        completed = subprocess.run(["node", "-e", script, str(reader), str(ROOT / "local" / "config.json")],
                                   capture_output=True, text=True, encoding="utf-8", check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "9236")
        hostile_environment = {**os.environ, "CITYHEAVEN_DAILY_CDP_PORT": "9225"}
        completed = subprocess.run(["node", "-e", script, str(reader), str(ROOT / "local" / "config.json")],
                                   capture_output=True, text=True, encoding="utf-8", check=False,
                                   env=hostile_environment)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "9236")
        with mock.patch.object(metrics.subprocess, "run", side_effect=[
            mock.Mock(returncode=0, stdout=json.dumps({
                "cdp_reachable": True, "runtime_identity": "MATCH", "profile_identity": "MATCH",
                "cdp_port": 9227, "auth_state": "AUTHENTICATED", "daily_manager_ready": True,
                "existing_process_reused": True, "additional_browser_launch_count": 0,
            }), stderr=""),
            mock.Mock(returncode=0, stdout='{"casts":[]}', stderr=""),
        ]) as called:
            metrics.fetch("2026-09-24")
        self.assertEqual(called.call_count, 2)
        command = called.call_args_list[1].args[0]
        self.assertEqual(command[-2:], ["--cdp-port", "9227"])
        self.assertNotIn("--config", command)
        self.assertNotIn("9225", command)

    def test_auth_required_classification_from_single_probe(self):
        module = ROOT / "src" / "windows_execution_foundation" / "cityheaven_daily_opera_runtime.js"
        source = module.read_text(encoding="utf-8")
        self.assertIn("'AUTH_REQUIRED'", source)
        self.assertIn("Runtime.evaluate", source)
        for forbidden in (".click(", ".submit(", "method: 'POST'", "Network.setCookie"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
