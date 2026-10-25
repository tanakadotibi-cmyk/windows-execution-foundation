'use strict';
const assert = require('node:assert/strict');
const {recoverCity,connect} = require('../src/windows_execution_foundation/daily_saved_login_runtime');
async function scenario({reject = false, mismatch = false, already = false, empty = false} = {}) {
  let submissions=0,created=0,closed=0;
  const dependencies={
    inspectPort: async()=>({state:mismatch?'MISMATCH':'MATCH'}),
    fetchJson:async()=>({webSocketDebuggerUrl:'browser'}),
    createPageTarget:async()=>{created++;return 'owned';},
    listTargets:async()=>[{id:'owned',webSocketDebuggerUrl:'page'}],
    command:async(url,method,args)=>{assert.equal(args.targetId,'owned');closed++;return {success:true};},
    wait:async()=>{},
    connect:(url,evidence)=>({close:()=>{},call:async(method,args={})=>{
      if(method!=='Runtime.evaluate')return {};
      if(args.expression.startsWith('(async()=>'))return {result:{value:{state:reject?'AUTH_REQUIRED':'AUTHENTICATED'}}};
      if(args.expression.startsWith('({origin'))return {result:{value:{origin:'https://newmanager.cityheaven.net',ready:'complete',path:(already||submissions&&!reject)?'/C2GirlList.php':'/C1Login.php',manager:already||submissions&&!reject}}};
      if(args.expression.endsWith('("city",false)'))return {result:{value:{code:empty?'SAVED_FIELDS_EMPTY':'READY'}}};
      submissions++;return {result:{value:{code:'SUBMITTED',submitted:true}}};
    }}),
  };
  const result=await recoverCity({cdpPort:9227},dependencies);
  return {result,submissions,created,closed};
}
(async()=>{
  let r=await scenario();assert.equal(r.result.status,'AUTHENTICATED');assert.equal(r.submissions,1);assert.equal(r.closed,1);
  r=await scenario({reject:true});assert.equal(r.result.status,'OWNER_AUTH_REQUIRED');assert.equal(r.submissions,1);assert.equal(r.closed,1);
  r=await scenario({empty:true});assert.equal(r.submissions,0);assert.equal(r.closed,1);
  r=await scenario({already:true});assert.equal(r.result.status,'AUTHENTICATED');assert.equal(r.submissions,0);
  r=await scenario({mismatch:true});assert.equal(r.created,0);assert.equal(r.submissions,0);
  const original=global.WebSocket;let socket,acks=0;
  global.WebSocket=class {
    constructor(){socket=this;queueMicrotask(()=>this.onopen());}
    send(raw){const item=JSON.parse(raw);if(item.method==='Page.handleJavaScriptDialog')acks++;queueMicrotask(()=>this.onmessage({data:JSON.stringify({id:item.id,result:{}})}));}
    close(){}
  };
  try{
    const evidence={logout_notice_ack_count:0,submit_count:0};const client=connect('fake',evidence);
    await client.call('Page.enable');
    const event={method:'Page.javascriptDialogOpening',params:{type:'alert',message:'ログアウトしました。もう一度ログインしてください。'}};
    await socket.onmessage({data:JSON.stringify(event)});
    assert.equal(acks,1);assert.equal(evidence.logout_notice_ack_count,1);
    await socket.onmessage({data:JSON.stringify(event)});
    assert.equal(acks,1);assert.equal(evidence.blocked,'OWNER_AUTH_REQUIRED');client.close();
  }finally{global.WebSocket=original;}
  console.log('PASS: one logout OK; one login; failed/uncertain login does not resubmit; exact owned tab cleanup; wrong profile never opens a tab');
})().catch(error=>{console.error(error);process.exitCode=1;});
