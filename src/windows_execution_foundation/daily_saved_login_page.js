function savedLoginPage(site, submit) {
  // Runs only in an owned daily-collector tab. Never returns field values.
  const origins = {cti: 'https://cti2.fuzoku-fan.jp', city: 'https://newmanager.cityheaven.net'};
  const result = code => ({code, submitted: false});
  if (location.origin !== origins[site]) return result('ORIGIN_MISMATCH');
  const visible = e => Boolean(e && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
  if ([...document.querySelectorAll('iframe[src*="captcha" i], [class*="captcha" i], input[autocomplete="one-time-code"], input[name*="otp" i]')].some(visible)) return result('OWNER_AUTH_REQUIRED');
  if (site === 'cti' && !(location.pathname.startsWith('/office/') || (location.pathname === '/top/' && /^#login(?:\/|$)/.test(location.hash)))) return result('LOGIN_ROUTE_MISMATCH');
  if (site === 'city' && location.pathname !== '/C1Login.php') return result('NOT_LOGIN_PAGE');
  const forms = [...document.forms].filter(f => visible(f) && [...f.querySelectorAll('input[type="password"]')].some(visible));
  if (forms.length !== 1) return result('LOGIN_FORM_NOT_UNIQUE');
  const form = forms[0];
  if (new URL(form.action || location.href, location.href).origin !== origins[site]) return result('FORM_ORIGIN_MISMATCH');
  // CTI's observed staff form also contains a dummypass input. Submit only the
  // real, browser-filled password control; never populate the dummy field.
  const passwords = [...form.querySelectorAll('input[type="password"]')].filter(e => visible(e) && !e.disabled && (site !== 'cti' || e.name === 'password'));
  if (passwords.length !== 1 || !passwords[0].value) return result('SAVED_FIELDS_EMPTY');
  const fields = [...form.querySelectorAll('input, select')].filter(e => visible(e) && !e.disabled && !(site === 'cti' && e.name === 'dummypass') && !['submit','button','checkbox','radio','hidden'].includes(e.type));
  if (fields.some(e => !String(e.value || '').trim())) return result('SAVED_FIELDS_EMPTY');
  const buttons = [...form.querySelectorAll('button,input[type="submit"],input[type="button"]')].filter(e => visible(e) && !e.disabled && /^(ログイン|Login|Sign in|ログオン)$/i.test(String(e.innerText || e.textContent || e.value || '').trim()));
  if (buttons.length !== 1) return result('LOGIN_BUTTON_NOT_UNIQUE');
  if (!submit) return result('READY');
  // Ordinary form-button action; no credential assignment, loader removal or form bypass.
  buttons[0].click();
  return {code: 'SUBMITTED', submitted: true};
}
