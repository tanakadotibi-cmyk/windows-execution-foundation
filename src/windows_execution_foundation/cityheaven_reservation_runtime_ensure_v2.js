'use strict';

const { setTimeout: delay } = require('node:timers/promises');

const reader = require('./cityheaven_reservation_runtime_reader.js');

function boundedInt(value, fallback, max) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > max) return fallback;
  return parsed;
}

function resolveAttempts(dependencies) {
  if (dependencies && Number.isInteger(dependencies.attempts)) {
    return boundedInt(dependencies.attempts, 3, 5);
  }
  return boundedInt(process.env.CITYHEAVEN_INSPECT_ATTEMPTS, 3, 5);
}

function resolveSpacing(dependencies) {
  if (dependencies && Number.isInteger(dependencies.spacingMs)) {
    return boundedInt(dependencies.spacingMs, 3000, 10000);
  }
  return boundedInt(process.env.CITYHEAVEN_INSPECT_SPACING_MS, 3000, 10000);
}

/**
 * Retry the existing inspectPort. A long-running Chrome without --enable-automation
 * is identified via chrome://version, and that read can miss the profile once.
 * commandLineMatches is set only after the Python preflight has already proved
 * the process command line is the reservation profile. It does not override a MATCH.
 */
async function inspectWithPatience(config, options = {}) {
  const inspect = options.inspectPort;
  if (typeof inspect !== 'function') throw Object.assign(new Error('CDP_PORT_RUNTIME_MISMATCH'), { runtimeCode: 'CDP_PORT_RUNTIME_MISMATCH' });
  if (process.env.CITYHEAVEN_UNIT_TEST === '1' && (inspect === reader.inspectPort || inspect === reader.dailyReadiness)) {
    throw Object.assign(new Error('CITYHEAVEN_UNIT_TEST blocks real CDP'), { runtimeCode: 'CDP_PORT_RUNTIME_MISMATCH' });
  }
  const wait = options.wait || delay;
  const attempts = resolveAttempts(options);
  const spacingMs = resolveSpacing(options);
  let last = { state: 'ABSENT' };
  for (let index = 0; index < attempts; index += 1) {
    last = await inspect(config);
    if (last && last.state === 'MATCH') return last;
    if (index + 1 < attempts) await wait(spacingMs);
  }
  if (options.commandLineMatches === true && (!last || last.state !== 'MATCH')) {
    return { state: 'MATCH', profile_source: 'COMMAND_LINE' };
  }
  return last || { state: 'MISMATCH' };
}

async function dailyReadinessV2(rawConfig, dependencies = {}) {
  const hasInspect = dependencies && typeof dependencies.inspectPort === 'function';
  const hasReadiness = dependencies && typeof dependencies.dailyReadiness === 'function';
  if (process.env.CITYHEAVEN_UNIT_TEST === '1' && (!hasInspect || !hasReadiness)) {
    throw Object.assign(new Error('CITYHEAVEN_UNIT_TEST blocks real CDP'), {
      runtimeCode: 'CDP_PORT_RUNTIME_MISMATCH',
    });
  }
  const dailyReadiness = dependencies.dailyReadiness || reader.dailyReadiness;
  const inspectPort = dependencies.inspectPort || (config => reader.inspectPort(config));
  const wait = dependencies.wait || delay;
  const commandLineMatches = dependencies.commandLineMatches === true
    || process.env.CITYHEAVEN_PROFILE_CMDLINE_MATCH === '1';
  const attempts = resolveAttempts(dependencies);
  const spacingMs = resolveSpacing(dependencies);
  async function inspect(config) {
    return inspectWithPatience(config, {
      inspectPort: () => inspectPort(config),
      wait,
      attempts,
      spacingMs,
      commandLineMatches,
    });
  }
  // Pass only inspect. The original dailyReadiness still owns launch and page readiness.
  return dailyReadiness(rawConfig, { inspect });
}

module.exports = { inspectWithPatience, dailyReadinessV2 };
