'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../src/windows_execution_foundation/daily_saved_login_page.js'), 'utf8');
function fixture({site = 'city', empty = false, wrongOrigin = false, crossOriginForm = false, challenge = false, duplicate = false} = {}) {
  let clicks = 0;
  const visible = {getClientRects: () => [1], disabled: false};
  const password = {...visible, type: 'password', name:'password', value: empty ? '' : 'TEST_SECRET_NOT_FOR_OUTPUT'};
  const dummy = {...visible, type:'password', name:'dummypass', value:''};
  const username = {...visible, type: 'text', value: 'test-account'};
  const button = {...visible, innerText: 'ログイン', click: () => {clicks++;}};
  const origin = site === 'city' ? 'https://newmanager.cityheaven.net' : 'https://cti2.fuzoku-fan.jp';
  const form = {...visible, action: crossOriginForm ? 'https://other.invalid/login' : origin + '/login', querySelectorAll: selector => {
    if (selector === 'input[type="password"]') return site === 'cti' ? [dummy,password] : [password];
    if (selector === 'input, select') return site === 'cti' ? [dummy,username,password] : [username,password];
    return duplicate ? [button, button] : [button];
  }};
  const context = {URL, location: {origin: wrongOrigin ? 'https://other.invalid' : origin, pathname: site === 'city' ? '/C1Login.php' : '/top/', hash:'#login/staff', href: origin + '/login'}, document: {forms: [form], querySelectorAll: () => challenge ? [visible] : []}, getComputedStyle: () => ({visibility: 'visible'})};
  return {run: submit => vm.runInNewContext('(' + source + ')', context)(site, submit), clicks: () => clicks};
}
for (const site of ['cti', 'city']) {
  const f = fixture({site});
  assert.equal(f.run(false).code, 'READY'); assert.equal(f.clicks(), 0);
  const result = f.run(true); assert.equal(result.submitted, true); assert.equal(f.clicks(), 1);
  assert.ok(!JSON.stringify(result).includes('TEST_SECRET'));
}
for (const [options, code] of [[{empty:true},'SAVED_FIELDS_EMPTY'],[{wrongOrigin:true},'ORIGIN_MISMATCH'],[{crossOriginForm:true},'FORM_ORIGIN_MISMATCH'],[{challenge:true},'OWNER_AUTH_REQUIRED'],[{duplicate:true},'LOGIN_BUTTON_NOT_UNIQUE']]) {
  const f=fixture(options); assert.equal(f.run(true).code,code); assert.equal(f.clicks(),0);
}
console.log('PASS: both sites submit once; empty fields, wrong origin, foreign form, challenge and ambiguous button never submit; no secret output');
