'use strict';
const assert = require('node:assert/strict');
const {pageAuthState} = require('../src/windows_execution_foundation/cityheaven_daily_reader.js');
const manager = 'https://newmanager.cityheaven.net/C2GirlList.php?shopdir=s_apronlady';
const login = 'https://newmanager.cityheaven.net/C1Login.php';
const base = {requestedUrl: manager, responseUrl: manager, documentUrl: login,
  hasManagerStructure: true, hasLoginForm: false};
assert.equal(pageAuthState(base), 'AUTHENTICATED');
assert.equal(pageAuthState({...base, responseUrl: login}), 'AUTH_REQUIRED');
assert.equal(pageAuthState({...base, responseUrl: '', hasManagerStructure: false}), 'AUTH_REQUIRED');
assert.equal(pageAuthState({...base, hasManagerStructure: false, hasLoginForm: true}), 'AUTH_REQUIRED');
assert.equal(pageAuthState({...base, hasManagerStructure: false}), 'SOURCE_MISSING');
console.log('PASS: fetched response authentication and real login redirects');
