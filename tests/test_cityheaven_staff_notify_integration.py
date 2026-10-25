from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class CityHeavenStaffNotifyIntegrationTests(unittest.TestCase):
    def test_only_confirmed_unreplied_candidate_invokes_staff_notification(self):
        root = Path(__file__).resolve().parents[1]
        core = root / "src" / "windows_execution_foundation" / "cityheaven_reservation_once.js"
        with tempfile.TemporaryDirectory() as directory:
            script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const core = require(process.argv[1]);
const root = process.argv[2];
const readerPath = path.join(root, 'reader.js');
fs.writeFileSync(readerPath, 'exports.BrowserReader=class {};\n', 'utf8');
const policy = {version:'v',rules:{message_send_enabled:false},
  template_a:{course_exact:'A course',text:'canonical A'},template_b:{text:'canonical B'}};
const row = {startTime:'13:00',cast:'CAST-A',status:'\u78ba\u5b9a',rsvId:'private-rsv-id'};
async function invoke(name, history) {
  const receiptPath = path.join(root, `${name}.receipt.json`);
  const statePath = path.join(root, `${name}.send-state.json`);
  const manifestPath = path.join(root, `${name}.manifest.json`);
  const rawKey = 'raw-guard-key-PII-never-forward';
  fs.writeFileSync(manifestPath, JSON.stringify({reservation_key:rawKey,
    reservation_at:'2026-09-28T04:00:00Z',cast:'CAST-A',course:'HINT',request_hash:'a'.repeat(64),
    receipt_path:receiptPath,reader_path:readerPath,send_state_path:statePath,
    staff_notify_enabled:true}), 'utf8');
  const notified = [];
  const reader = {async ensure(){},async city(){return [row];}};
  class Adapter { async reservationDetail(){return {course:'A course'};}
    async chatCandidates(){return [{unread:false}];} async chatHistory(){return {messages:history,complete:true,authenticated:true};} }
  const code = await core.run(manifestPath,{reader,Adapter,loadPolicy:async()=>policy,now:()=>new Date('2026-09-28T03:00:00Z'),
    notifyStaff:async(key,occurredAt,state)=>{
      notified.push({key,occurredAt,state});
      return {status:'notified',reason_code:'STAFF_NOTIFIED'};
    }});
  return {code,notified,receipt:JSON.parse(fs.readFileSync(receiptPath,'utf8')),rawKey,statePath};
}
(async()=>{
  const replied = await invoke('replied',[{text:'canonical A',sender_role:'staff',rsvId:'private-rsv-id'}]);
  const unreplied = await invoke('unreplied',[]);
  assert.equal(replied.notified.length,0);
  assert.equal(replied.receipt.reason_code,'DONE_NO_SEND');
  assert.equal(unreplied.notified.length,1);
  assert.equal(unreplied.notified[0].key,core.staffReservationKey(unreplied.rawKey));
  assert.ok(unreplied.notified[0].key.startsWith('ch_'));
  assert.ok(!JSON.stringify(unreplied.notified[0]).includes(unreplied.rawKey));
  assert.equal(unreplied.receipt.reason_code,'STAFF_NOTIFIED');
  for (const receipt of [replied.receipt,unreplied.receipt]) {
    assert.equal(receipt.customer_send_count,0);
    assert.equal(receipt.reservation_mutation_count,0);
    assert.equal(receipt.external_change_count,0);
  }
  process.stdout.write(JSON.stringify({status:'PASS'}));
})().catch(error=>{console.error(error);process.exitCode=1});
"""
            completed = subprocess.run(
                ["node", "-e", script, str(core), directory], capture_output=True, text=True,
                encoding="utf-8", check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"status": "PASS"})


if __name__ == "__main__":
    unittest.main()
