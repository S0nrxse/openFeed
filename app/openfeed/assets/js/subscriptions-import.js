/* Settings -> Account -> Your data -> Import OPML. Subscribes the current profile only
   (POST /api/import/subscriptions.opml); the server fetches the new feeds afterwards. */
(() => {
  'use strict';

  const toast = (message, options) => window.ofToast?.(message, options);

  async function refreshSidebar() {
    const sidebar = document.querySelector('.desktop-sidebar');
    if (!sidebar) return;
    try {
      const response = await fetch('/api/sidebar/subscriptions', { headers: { Accept: 'text/html' } });
      if (!response.ok) return;
      const markup = (await response.text()).trim();
      const current = sidebar.querySelector('[data-subscription-nav]');
      const parsed = markup ? document.createRange().createContextualFragment(markup).firstElementChild : null;
      if (current && parsed) current.replaceWith(parsed);
      else if (parsed) sidebar.querySelector('.sidebar-meta')?.before(parsed);
    } catch (error) { /* the list catches up on the next page load */ }
  }

  document.addEventListener('change', async (event) => {
    const input = event.target.closest?.('[data-opml-file]');
    if (!input || !input.files || !input.files[0]) return;
    const label = input.closest('[data-opml-import]');
    const body = new FormData();
    body.append('file', input.files[0]);
    label?.setAttribute('aria-busy', 'true');
    input.disabled = true;
    try {
      const response = await fetch('/api/import/subscriptions.opml', { method: 'POST', body, credentials: 'same-origin' });
      let data = {};
      try { data = await response.json(); } catch (error) { data = {}; }
      if (!response.ok || data.ok !== true) {
        throw new Error(typeof data.detail === 'string' ? data.detail : `The server answered HTTP ${response.status}. Nothing was changed.`);
      }
      const added = Number(data.subscribed || 0);
      const already = Number(data.already || 0);
      const parts = [`Subscribed to ${added} new feed${added === 1 ? '' : 's'}`];
      if (already) parts.push(`${already} already followed`);
      toast(`${parts.join(', ')}. New videos arrive over the next few minutes.`);
      refreshSidebar();
    } catch (error) {
      toast(error.message || 'Could not import that file.', { kind: 'error' });
    } finally {
      input.value = '';
      input.disabled = false;
      label?.removeAttribute('aria-busy');
    }
  });
})();
