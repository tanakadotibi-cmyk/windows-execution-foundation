const assert = require('node:assert/strict');
const { classifyResult, EXPECTED_ORIGIN, MANAGER_PATH, LOGIN_PATH } = require('../src/windows_execution_foundation/cityheaven_daily_keepalive.js');
for (const status of [403, 429, 500]) {
  assert.notEqual(classifyResult({http_status: status, origin: EXPECTED_ORIGIN, pathname: MANAGER_PATH}).auth_class, 'AUTHENTICATED');
}
assert.equal(classifyResult({http_status: 200, origin: EXPECTED_ORIGIN, pathname: MANAGER_PATH}).auth_class, 'AUTHENTICATED');
assert.notEqual(classifyResult({http_status: 200, origin: EXPECTED_ORIGIN, pathname: LOGIN_PATH}).auth_class, 'AUTHENTICATED');
console.log('keepalive: 5 cases passed');
