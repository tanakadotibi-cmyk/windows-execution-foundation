'use strict';

class CityHeavenChatReadOnlyAdapter {
  constructor(reader) { this.reader = reader; }

  async reservationDetail(row) {
    if (typeof this.reader.reservationDetail === 'function') return this.reader.reservationDetail(row);
    return this.reader.read('city', async function reservationDetailInPage(rsvId) {
      const response = await fetch(`/reservation/detail?rsvId=${encodeURIComponent(rsvId)}`, {
        method: 'GET', credentials: 'include', headers: { Accept: 'text/html' },
      });
      if (!response.ok || response.redirected) return null;
      const document_ = new DOMParser().parseFromString(await response.text(), 'text/html');
      const compact = value => String(value || '').replace(/[\s\u3000]+/g, ' ').trim();
      const labeled = labels => {
        for (const element of document_.querySelectorAll('th,dt,label,.label,.title')) {
          if (!labels.some(label => compact(element.textContent).includes(label))) continue;
          const target = element.matches('th') ? element.nextElementSibling
            : element.matches('dt') ? element.nextElementSibling
              : element.control || element.nextElementSibling;
          const value = compact(target?.value || target?.textContent);
          if (value) return value;
        }
        return '';
      };
      const identifiers = [
        labeled(['お名前', '顧客名', 'お客様名']), labeled(['電話番号', 'TEL']),
        labeled(['メール', 'E-mail']), labeled(['my_heaven_id', '会員ID']),
      ].filter(Boolean);
      const candidates = [];
      if (!identifiers.length) return { course: labeled(['コース']), chatCandidates: candidates };
      for (const element of document_.querySelectorAll('form[action*="DisplayChat" i],a[href*="DisplayChat" i]')) {
        const container = element.closest('tr,li,.chat-item,.customer-item') || element;
        const haystack = compact(container.textContent);
        if (!identifiers.length || !identifiers.every(value => haystack.includes(value))) continue;
        const rawUnread = element.getAttribute('data-unread-count') ?? container.getAttribute('data-unread-count')
          ?? element.getAttribute('data-unread') ?? container.getAttribute('data-unread');
        const unread = rawUnread === '0' || rawUnread === 'false' ? false
          : rawUnread && rawUnread !== '0' && rawUnread !== 'false' ? true : null;
        const form = element.matches('form') ? element : null;
        const url = new URL(form?.getAttribute('action') || element.getAttribute('href'), location.origin).href;
        const method = String(form?.method || 'GET').toUpperCase();
        const body = form ? [...new FormData(form).entries()] : [];
        candidates.push({ url, method, body, unread });
      }
      return { course: labeled(['コース']), chatCandidates: candidates };
    }, row.rsvId);
  }

  async chatCandidates(detail) {
    if (typeof this.reader.chatCandidates === 'function') return this.reader.chatCandidates(detail);
    return Array.isArray(detail?.chatCandidates) ? detail.chatCandidates : [];
  }

  async chatHistory(candidate) {
    if (typeof this.reader.chatHistory === 'function') return this.reader.chatHistory(candidate);
    return this.reader.read('city', async function chatHistoryInPage(descriptor) {
      let url;
      try { url = new URL(descriptor?.url); } catch { return null; }
      if (url.origin !== location.origin || !/DisplayChat/i.test(url.pathname) || /Save/i.test(url.pathname)) return null;
      const method = descriptor.method === 'POST' ? 'POST' : 'GET';
      const body = method === 'POST' ? new URLSearchParams(Array.isArray(descriptor.body) ? descriptor.body : []) : undefined;
      const response = await fetch(url.href, {
        method, body, credentials: 'include', headers: { Accept: 'application/json,text/html' },
      });
      if (!response.ok || response.redirected) return null;
      const text = await response.text();
      try {
        const value = JSON.parse(text);
        const list = Array.isArray(value) ? value : value.view_list || value.messages || value.list;
        if (!Array.isArray(list)) return null;
        // Preserve source metadata. Bare arrays/text never prove completeness
        // or authentication; do not silently discard malformed messages.
        return { messages: list, authenticated: value.authenticated === true,
          complete: value.complete === true, has_more: value.has_more,
          next_cursor: value.next_cursor };
      } catch {
        const document_ = new DOMParser().parseFromString(text, 'text/html');
        const nodes = [...document_.querySelectorAll('[data-message-id],.message-item,.chat-message,.message')];
        // No accepted HTML completeness/sender/reservation schema is available.
        // Keep this read UNKNOWN rather than treating a login/partial page as empty.
        return { messages: nodes.map(node => ({ text: node.innerText || node.textContent || '',
          sender_role: node.getAttribute('data-sender-role'),
          reservation_id: node.getAttribute('data-reservation-id'),
          sent_at: node.getAttribute('data-sent-at') })), authenticated: false, complete: false };
      }
    }, candidate);
  }

  async sendMessage() { throw new Error('CUSTOMER_SEND_DISABLED'); }
}

module.exports = { CityHeavenChatReadOnlyAdapter };
