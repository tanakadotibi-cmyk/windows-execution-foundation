const assert=require('node:assert/strict');
const runtime=require('../src/windows_execution_foundation/cityheaven_reservation_runtime_reader.js');
const raw={browser:'CHROME',cdp_port:9227,chrome_executable:'C:\\Chrome\\chrome.exe',profile_path:'C:\\Profiles\\Reservation',base_reader_path:__filename,startup_lock_path:'C:\\Locks\\existing.lock',restore_session:true};
const config=runtime.runtimeConfig(raw);
const cms={type:'page',url:'https://newmanager.cityheaven.net/C1Main.php',webSocketDebuggerUrl:'ws://local'};
(async()=>{
  let launchArgs,launchOptions;
  await runtime.launchChrome(config,{exists:()=>true,spawn:(exe,args,options)=>{
    launchArgs=args;launchOptions=options;
    return {once:(event,callback)=>{if(event==='spawn')queueMicrotask(callback);},unref:()=>{}};
  }});
  assert(launchArgs.includes('--restore-last-session'));
  assert(!launchArgs.includes(config.startUrl));
  assert.equal(launchOptions.windowsHide,true);
  assert.equal(runtime.runtimeConfig({...raw,restore_session:undefined}).restoreSession,false);

  let reads=0,waits=0;
  const readiness=await runtime.dailyManagerReadiness(config,{
    listTargets:async()=>[cms],wait:async()=>{waits++;},
    command:async()=>({result:{value:++reads===1?{origin:'null',ready:'complete'}:reads===2?{origin:'https://newmanager.cityheaven.net',ready:'loading'}:{origin:'https://newmanager.cityheaven.net',ready:'complete'}}}),
    createTarget:async()=>{throw Error('UNEXPECTED_CREATE');},
  });
  assert.equal(readiness.ready,true);assert.equal(reads,3);assert.equal(waits,2);
  let lists=0,creates=0;
  await runtime.dailyManagerReadiness(config,{
    listTargets:async()=>++lists===1?[]:[cms],createTarget:async()=>{creates++;},wait:async()=>{},
    command:async()=>({result:{value:{origin:'https://newmanager.cityheaven.net',ready:'complete'}}}),
  });
  assert.equal(creates,1);
  creates=0;lists=0;
  await assert.rejects(runtime.dailyManagerReadiness(config,{
    listTargets:async()=>{lists++;return [];},createTarget:async()=>{creates++;},wait:async()=>{},
  }),e=>e.runtimeCode==='BROWSER_CDP_UNAVAILABLE');
  assert.equal(creates,1);assert.equal(lists,16);
  console.log('cold-start: launch flags, unchanged reservation default, committed origin/load wait, single create, bounded failure passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
