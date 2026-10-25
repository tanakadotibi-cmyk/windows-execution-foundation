'use strict';

process.env.CITYHEAVEN_UNIT_TEST = '1';

const assert = require('node:assert');
const v2 = require('../src/windows_execution_foundation/cityheaven_reservation_runtime_ensure_v2.js');

async function main() {
  let calls = 0;
  const waits = [];
  const matched = await v2.inspectWithPatience({}, {
    attempts: 3,
    spacingMs: 3000,
    wait: async (ms) => { waits.push(ms); },
    inspectPort: async () => {
      calls += 1;
      return { state: calls >= 2 ? 'MATCH' : 'MISMATCH' };
    },
  });
  assert.strictEqual(matched.state, 'MATCH');
  assert.strictEqual(calls, 2);
  assert.deepStrictEqual(waits, [3000]);

  calls = 0;
  const trusted = await v2.inspectWithPatience({}, {
    attempts: 3,
    spacingMs: 4,
    commandLineMatches: true,
    wait: async () => {},
    inspectPort: async () => {
      calls += 1;
      return { state: 'MISMATCH' };
    },
  });
  assert.strictEqual(trusted.state, 'MATCH');
  assert.strictEqual(trusted.profile_source, 'COMMAND_LINE');
  assert.strictEqual(calls, 3);

  const untrusted = await v2.inspectWithPatience({}, {
    attempts: 2,
    spacingMs: 1,
    commandLineMatches: false,
    wait: async () => {},
    inspectPort: async () => ({ state: 'MISMATCH' }),
  });
  assert.strictEqual(untrusted.state, 'MISMATCH');
  assert.strictEqual(untrusted.profile_source, undefined);

  calls = 0;
  const viaReadiness = await v2.dailyReadinessV2({}, {
    attempts: 3,
    spacingMs: 1,
    commandLineMatches: true,
    wait: async () => {},
    inspectPort: async () => {
      calls += 1;
      return { state: 'MISMATCH' };
    },
    dailyReadiness: async (_raw, deps) => deps.inspect({}),
  });
  assert.strictEqual(viaReadiness.state, 'MATCH');
  assert.strictEqual(viaReadiness.profile_source, 'COMMAND_LINE');
  assert.strictEqual(calls, 3);

  calls = 0;
  const realMismatch = await v2.dailyReadinessV2({}, {
    attempts: 2,
    spacingMs: 1,
    commandLineMatches: false,
    wait: async () => {},
    inspectPort: async () => {
      calls += 1;
      return { state: 'MISMATCH' };
    },
    dailyReadiness: async (_raw, deps) => deps.inspect({}),
  });
  assert.strictEqual(realMismatch.state, 'MISMATCH');
  assert.strictEqual(calls, 2);

  await assert.rejects(
    () => v2.dailyReadinessV2({ cdp_port: 9227 }),
    /CITYHEAVEN_UNIT_TEST blocks real CDP/,
  );
  await assert.rejects(
    () => v2.inspectWithPatience({}, { inspectPort: require('../src/windows_execution_foundation/cityheaven_reservation_runtime_reader.js').inspectPort }),
    /CITYHEAVEN_UNIT_TEST blocks real CDP/,
  );
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
