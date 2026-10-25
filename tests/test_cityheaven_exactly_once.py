from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class CityHeavenExactlyOnceTests(unittest.TestCase):
    def test_required_send_gate_matrix(self):
        root = Path(__file__).resolve().parents[1]
        core = root / "src" / "windows_execution_foundation" / "cityheaven_reservation_once.js"
        with tempfile.TemporaryDirectory() as directory:
            script = r"""
const fs = require('node:fs');
const path = require('node:path');
const core = require(process.argv[1]);
const root = process.argv[2];
const readerPath = path.join(root, 'reader.js');
fs.writeFileSync(readerPath, 'class BrowserReader {}\nmodule.exports={BrowserReader};\n', 'utf8');
const confirmed = '\u78ba\u5b9a';
const row = {startTime:'13:00',cast:'CAST-A',status:confirmed,rsvId:'private-reservation-id'};
const policies = {
  A:{version:'v',rules:{message_send_enabled:true},template_a:{course_exact:'A course',text:'canonical A'},template_b:{text:'canonical B'}},
  B:{version:'v',rules:{message_send_enabled:true},template_a:{course_exact:'A course',text:'canonical A'},template_b:{text:'canonical B'}},
};
let sequence = 0;
async function invoke(name, options={}) {
  const receiptPath = path.join(root, `${name}-${++sequence}.receipt.json`);
  const statePath = options.statePath || path.join(root, `${name}.send-state.json`);
  const manifestPath = path.join(root, `${name}-${sequence}.manifest.json`);
  fs.writeFileSync(manifestPath, JSON.stringify({reservation_key:options.key || `cityheaven|reservation|${name}`,
    reservation_at:'2026-09-27T04:00:00Z',cast:'CAST-A',course:'HINT',request_hash:'a'.repeat(64),
    receipt_path:receiptPath,reader_path:readerPath,send_state_path:statePath}), 'utf8');
  const counts = options.counts || {send:0};
  const reader = {async ensure(){},async city(){return [row];}};
  class Adapter {
    async reservationDetail(){return {course:options.template === 'B' ? 'other course' : 'A course'};}
    async chatCandidates(){if(options.candidatesError) throw new Error('fetch'); return options.candidates ?? [{unread:false}];}
    async chatHistory(){if(options.historyError) throw new Error('fetch'); return {messages:options.history ?? [],complete:true,authenticated:true};}
    async sendMessage(){counts.send++; return options.uncertain ? null : {success:true,messageId:`mock-${counts.send}`};}
  }
  const code = await core.run(manifestPath,{reader,Adapter,loadPolicy:async()=>policies[options.template || 'A'],now:()=>new Date('2026-09-27T03:00:00Z')});
  return {code,receipt:JSON.parse(fs.readFileSync(receiptPath,'utf8')),counts,statePath};
}
(async()=>{
  const A = await invoke('a',{template:'A'});
  const B = await invoke('b',{template:'B'});
  const replay = await invoke('a-replay',{template:'A',key:'cityheaven|reservation|a',statePath:A.statePath,counts:A.counts});
  const replay10m = await invoke('a-replay-10m',{template:'A',key:'cityheaven|reservation|a',statePath:A.statePath,counts:A.counts});
  const sendingPath=path.join(root,'sending.send-state.json');
  fs.writeFileSync(sendingPath,JSON.stringify({schema_version:'cityheaven_send_state_v1',dedup_key:'cityheaven|reservation|sending',state:'SENDING',updated_at:new Date().toISOString()}));
  const sending=await invoke('sending',{key:'cityheaven|reservation|sending',statePath:sendingPath});
  const sendingRestart=await invoke('sending-restart',{key:'cityheaven|reservation|sending',statePath:sendingPath,counts:sending.counts});
  const sentPath=path.join(root,'sent.send-state.json');
  fs.writeFileSync(sentPath,JSON.stringify({schema_version:'cityheaven_send_state_v1',dedup_key:'cityheaven|reservation|sent',state:'SENT',updated_at:new Date().toISOString()}));
  const sent=await invoke('sent',{key:'cityheaven|reservation|sent',statePath:sentPath});
  const sentRestart=await invoke('sent-restart',{key:'cityheaven|reservation|sent',statePath:sentPath,counts:sent.counts});
  const uncertain=await invoke('uncertain',{uncertain:true});
  const uncertainReplay=await invoke('uncertain-replay',{key:'cityheaven|reservation|uncertain',statePath:uncertain.statePath,counts:uncertain.counts});
  const chatPresent=await invoke('present',{history:[{text:'different historical text'}]});
  const canonicalPresent=await invoke('canonical',{history:[{text:'canonical A',sender_role:'staff',rsvId:'private-reservation-id'}]});
  const fetchFailure=await invoke('fetch-failure',{historyError:true});
  const undetermined=await invoke('undetermined',{candidates:[]});
  const unread=await invoke('unread',{candidates:[{unread:true}]});
  process.stdout.write(JSON.stringify({A,B,replay,replay10m,sending,sendingRestart,sent,sentRestart,
    uncertain,uncertainReplay,chatPresent,canonicalPresent,fetchFailure,undetermined,unread}));
})().catch(error=>{console.error(error);process.exitCode=1});
"""
            completed = subprocess.run(
                ["node", "-e", script, str(core), directory], capture_output=True, text=True,
                encoding="utf-8", check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        # The old customer exactly-once cases now prove delivery is disabled,
        # including policy=true, prior SENDING/SENT and repeated invocations.
        for result in value.values():
            self.assertEqual(result["counts"]["send"], 0)
            self.assertEqual(result["receipt"]["customer_send_count"], 0)
            self.assertFalse(result["receipt"]["send_enabled"])
            self.assertFalse(result["receipt"]["would_send"])
        for key in ("A", "B", "replay", "replay10m", "sending", "sendingRestart",
                    "sent", "sentRestart", "uncertain", "uncertainReplay", "chatPresent"):
            self.assertEqual(value[key]["receipt"]["reason_code"], "SEND_DISABLED")
        self.assertEqual(value["canonicalPresent"]["receipt"]["reason_code"], "DONE_NO_SEND")
        for key in ("fetchFailure", "undetermined", "unread"):
            self.assertEqual(value[key]["receipt"]["message_state"], "FAIL_CLOSED")
        serialized = completed.stdout.lower()
        for forbidden in ("private-reservation-id", "canonical a", "canonical b"):
            self.assertNotIn(forbidden, json.dumps({k: v["receipt"] for k, v in value.items()}).lower())


if __name__ == "__main__":
    unittest.main()
