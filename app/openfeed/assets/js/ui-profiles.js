/* Settings -> Profiles: add, edit and delete. Switching is a plain form POST to
   /profiles/select and needs no script. Every change reloads into the Profiles pane, because
   the account menu, the header face and the whole feed belong to the profile. */
(() => {
  'use strict';

  const pane = document.querySelector('[data-settings-pane-body="profiles"]');
  if (!pane) return;
  const toast = (message, options) => window.ofToast?.(message, options);

  async function send(method, path, body) {
    const response = await fetch(path, {
      method, credentials: 'same-origin', cache: 'no-store',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = {};
    try { data = await response.json(); } catch (error) { data = {}; }
    if (!response.ok || data.ok !== true) {
      const known = { kids_profile: 'Kids profiles cannot change profiles.', session_required: 'Profiles are managed from a signed-in browser.' };
      throw new Error(data.message || known[data.detail] || `The server answered HTTP ${response.status}. Nothing was changed.`);
    }
    return data;
  }

  function reloadIntoPane(message) {
    try { sessionStorage.setItem('openfeed-profile-toast', message); } catch (error) { /* toast is a nicety */ }
    window.location.assign(`${window.location.pathname}?settings=profiles`);
  }

  try {
    const pending = sessionStorage.getItem('openfeed-profile-toast');
    if (pending) {
      sessionStorage.removeItem('openfeed-profile-toast');
      window.setTimeout(() => toast(pending, { kind: 'success' }), 0);
    }
  } catch (error) { /* storage blocked */ }

  function fields(form) {
    const data = new FormData(form);
    const out = { name: String(data.get('name') || '').trim(), color: String(data.get('color') || '') };
    const kids = form.querySelector('[name="is_kids"]');
    if (kids) out.is_kids = kids.checked;
    return out;
  }

  function busy(form, on) {
    form.querySelectorAll('button, input').forEach((control) => { control.disabled = on; });
  }

  pane.addEventListener('click', async (event) => {
    const open = event.target.closest('[data-profile-edit-open]');
    if (open) {
      const form = pane.querySelector(`[data-profile-edit="${CSS.escape(open.dataset.profileEditOpen)}"]`);
      if (!form) return;
      pane.querySelectorAll('[data-profile-edit]').forEach((other) => { if (other !== form) other.hidden = true; });
      form.hidden = !form.hidden;
      open.setAttribute('aria-expanded', String(!form.hidden));
      if (!form.hidden) form.querySelector('[name="name"]')?.focus();
      return;
    }
    const cancel = event.target.closest('[data-profile-cancel]');
    if (cancel) {
      const form = cancel.closest('form');
      form.reset();
      form.hidden = true;
      return;
    }
    const remove = event.target.closest('[data-profile-delete]');
    if (remove) {
      const name = remove.dataset.profileName || 'this profile';
      const confirmed = await window.ofConfirm?.({
        title: `Delete ${name}?`,
        body: `Deletes ${name}'s subscriptions, history, continue watching, likes, playlists, collections, preferences, downloads, API tokens and recommendations. Other profiles keep theirs. This cannot be undone.`,
        confirmLabel: 'Delete profile',
        danger: 'filled',
      });
      if (!confirmed) return;
      remove.disabled = true;
      try {
        await send('DELETE', `/api/profiles/${encodeURIComponent(remove.dataset.profileDelete)}`);
        reloadIntoPane(`${name} deleted`);
      } catch (error) {
        remove.disabled = false;
        toast(error.message, { kind: 'error' });
      }
    }
  });

  pane.addEventListener('submit', async (event) => {
    const form = event.target;
    if (form.matches('[data-profile-add]')) {
      event.preventDefault();
      const body = fields(form);
      busy(form, true);
      try {
        await send('POST', '/api/profiles', body);
        reloadIntoPane(`${body.name} added`);
      } catch (error) {
        busy(form, false);
        toast(error.message, { kind: 'error' });
      }
    } else if (form.matches('[data-profile-edit]')) {
      event.preventDefault();
      const body = fields(form);
      busy(form, true);
      try {
        await send('PATCH', `/api/profiles/${encodeURIComponent(form.dataset.profileEdit)}`, body);
        reloadIntoPane(`${body.name} saved`);
      } catch (error) {
        busy(form, false);
        toast(error.message, { kind: 'error' });
      }
    }
  });
})();
