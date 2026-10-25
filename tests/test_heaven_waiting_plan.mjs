import test from 'node:test';
import assert from 'node:assert/strict';
import { planHeavenWaitingUpdates } from '../src/windows_execution_foundation/cti_shinchaku_plan.mjs';
const now=Date.parse('2026-10-15T16:00:00+09:00');
const config={office:'test',shop:'テスト店舗',targetIds:{'テスト':'1'},castAliases:{'★テスト':'テスト'},heavenWaitingRule:{enabled:true,siteIds:['STG420','STA413','ST2100','STZ116','ST8737','ST8542','ST7453']}};
function fixture(){return {observedAt:new Date(now).toISOString(),cti:{origin:'https://cti2.fuzoku-fan.jp',office:'test',businessDate:'20261015',reservations:[]},details:[],target:{origin:'https://si9.jp',account:'テスト店舗',remainingUpdates:100,targets:[{targetId:'1',name:'テスト',shift:['14:00','00:00']}]},heaven:[{targetId:'1',state:'待機中',detail:'15:01～待機中',application:'待機中',processing:false}]};}
const plan=(s,receipts=[])=>planHeavenWaitingUpdates(s,config,receipts,now);
test('waiting heading triggers one cast and all seven approved sites',()=>{const p=plan(fixture());assert.equal(p.candidates.length,1);assert.deepEqual(p.candidates[0].siteIds,config.heavenWaitingRule.siteIds);});
test('already immediate status is skipped even when detail contains waiting',()=>{const s=fixture();s.heaven[0].state='即ヒメ';assert.equal(plan(s).candidates.length,0);});
test('CTI in-room wins even after the scheduled end',()=>{const s=fixture();s.cti.reservations=[{reservationId:'r1',state:'入室',time:'14:01-15:16'}];s.details=[{reservationId:'r1',shop:'テスト店舗',cast:'★テスト',playState:'入室完了',time:'14:01-15:16'}];assert.equal(plan(s).held[0].reason,'CTI_IN_ROOM_TAKES_PRIORITY');});
test('unidentified in-room cast blocks waiting updates',()=>{const s=fixture();s.cti.reservations=[{reservationId:'r1',state:'入室'}];assert.equal(plan(s).held[0].reason,'ALL_IN_ROOM_CASTS_MUST_BE_IDENTIFIED');});
test('unknown outcome prevents repeated sends',()=>{assert.equal(plan(fixture(),[{rule:'HEAVEN_WAITING',targetId:'1',result:'PENDING'}]).held[0].reason,'PREVIOUS_SEND_REQUIRES_RECONCILIATION');});
test('after cooldown a newly observed waiting state can trigger again',()=>{assert.equal(plan(fixture(),[{rule:'HEAVEN_WAITING',targetId:'1',result:'VERIFIED',submittedAt:new Date(now-1800000).toISOString()}]).candidates.length,1);});
test('a verified Heaven transition allows the next waiting episode without claiming other sites verified',()=>{assert.equal(plan(fixture(),[{rule:'HEAVEN_WAITING',targetId:'1',result:'PARTIAL',triggerResult:'VERIFIED',submittedAt:new Date(now-1800000).toISOString()}]).candidates.length,1);});
test('unknown state, busy application, stale reading and exhausted quota do not send',()=>{for(const mutate of [s=>s.heaven[0].state=null,s=>s.heaven[0].application='接客中',s=>s.observedAt=new Date(now-120001).toISOString(),s=>s.target.remainingUpdates=0]){const s=fixture();mutate(s);assert.equal(plan(s).candidates.length,0);}});
