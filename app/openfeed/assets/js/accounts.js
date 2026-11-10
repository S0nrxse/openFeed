/* Account & security and Admin, Users (account_pages.py). The pages are plain forms that
   work without this; it adds the shared confirm dialog before destructive submits and copy
   buttons for one-time links and recovery codes. */
(() => {
  'use strict';

  /* After a redirect home (/?done=kept from "Keep it in my account", /?done=fresh from
     "Start fresh"): a toast, then the address loses ?done= so a reload doesn't repeat it.
     Only these fixed sentences, so a crafted link cannot put words on the page; they match
     account_pages.NOTICES. */
  const HOME_NOTICES = {
    kept: 'The data from before sign-in is now in your account.',
    fresh: 'Starting fresh. The data from before sign-in stays where it was, unused.',
  };
  if (location.pathname === '/') {
    const params = new URLSearchParams(location.search);
    const message = HOME_NOTICES[params.get('done') || ''];
    if (message) {
      params.delete('done');
      const rest = params.toString();
      try { history.replaceState(history.state, '', `/${rest ? `?${rest}` : ''}${location.hash}`); } catch (error) { /* keep going */ }
      const show = () => { if (window.ofToast) window.ofToast(message, { kind: 'success', duration: 7000 }); };
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', show, { once: true });
      else window.setTimeout(show, 0);
    }
  }

  const page = document.querySelector('.acct-page');
  if (!page) return;

  /* Devices: sign-in times in this viewer's own time zone (the server writes UTC). */
  page.querySelectorAll('time[data-local-time]').forEach((node) => {
    const when = new Date(node.getAttribute('datetime') || '');
    if (Number.isNaN(when.getTime())) return;
    try {
      node.textContent = when.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    } catch (error) { /* keep the UTC text */ }
  });

  page.querySelectorAll('form[data-confirm-title]').forEach((form) => {
    form.addEventListener('submit', async (event) => {
      if (form.dataset.confirmed === '1' || !window.ofConfirm) return;
      event.preventDefault();
      const ok = await window.ofConfirm({
        title: form.dataset.confirmTitle,
        body: form.dataset.confirmBody || '',
        confirmLabel: form.dataset.confirmLabel || 'Confirm',
        danger: true,
      });
      if (!ok) return;
      form.dataset.confirmed = '1';
      form.requestSubmit ? form.requestSubmit() : form.submit();
    });
  });

  async function copy(text, button) {
    const label = button.textContent;
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = 'Copied';
    } catch (error) {
      button.textContent = 'Select and copy';
    }
    window.setTimeout(() => { button.textContent = label; }, 1800);
  }

  page.querySelectorAll('[data-copy-target]').forEach((button) => {
    const source = button.parentElement?.querySelector('[data-copy-text]');
    if (!source || !navigator.clipboard) return;
    button.hidden = false;
    button.addEventListener('click', () => copy(source.textContent.replace(/\s+/g, ' ').trim(), button));
  });

  page.querySelectorAll('[data-copy-codes]').forEach((button) => {
    if (!navigator.clipboard) return;
    button.hidden = false;
    button.addEventListener('click', () => copy(button.dataset.copyCodes, button));
  });
})();
