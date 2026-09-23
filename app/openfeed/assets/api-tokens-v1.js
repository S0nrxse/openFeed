(() => {
  'use strict';
  const dialog = document.querySelector('#settings-dialog');
  const pane = dialog?.querySelector('[data-settings-pane-body="api-tokens"]');
  const form = pane?.querySelector('[data-api-token-form]');
  if (!form) return;
  const list = pane.querySelector('[data-api-token-list]');
  const status = pane.querySelector('[data-api-token-status]');
  const result = pane.querySelector('[data-api-token-result]');
  const secret = pane.querySelector('[data-api-token-value]');
  const create = form.querySelector('[type="submit"]');
  const refresh = pane.querySelector('[data-api-token-refresh]');
  let generatedId = null;
  let loading = false;
  let creating = false;
  let generation = 0;

  function clearSecret() {
    secret.value = '';
    generatedId = null;
    result.hidden = true;
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      credentials: 'same-origin', cache: 'no-store', redirect: 'error',
      ...options, headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    });
    if (response.status === 401 || response.status === 403) {
      throw new Error('Sign in with Authentik again to manage your tokens.');
    }
    if (!response.ok) throw new Error('The request failed. Refresh the list before trying again.');
    const data = await response.json();
    if (data.ok !== true) throw new Error('The request failed. Refresh the list before trying again.');
    return data;
  }

  function render(items) {
    list.replaceChildren();
    if (!items.length) {
      const empty = document.createElement('p');
      empty.className = 'dialog-note';
      empty.textContent = 'No API tokens yet.';
      list.append(empty);
      return;
    }
    for (const item of items) {
      const row = document.createElement('div');
      row.className = 'api-token-row';
      const details = document.createElement('div');
      const name = document.createElement('strong');
      name.textContent = item.name || 'API token';
      const access = document.createElement('small');
      access.textContent = `${item.read_only ? 'Read only' : 'Read and write'} · ${item.prefix || ''}`;
      const lastUsed = document.createElement('small');
      const date = new Date(item.last_used_at);
      lastUsed.textContent = item.last_used_at && Number.isFinite(date.getTime())
        ? `Last used ${date.toLocaleString()}` : 'Never used';
      details.append(name, access, lastUsed);
      const revoke = document.createElement('button');
      revoke.type = 'button';
      revoke.className = 'text-action';
      revoke.textContent = 'Revoke';
      revoke.setAttribute('aria-label', `Revoke ${item.name || 'API token'}`);
      revoke.addEventListener('click', async () => {
        revoke.disabled = true;
        status.textContent = '';
        try {
          await request(`/api/tokens/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
          if (generatedId === item.id) clearSecret();
          row.remove();
          status.textContent = 'Token revoked.';
          if (!list.children.length) render([]);
        } catch (error) {
          status.textContent = error.message || 'Could not revoke token.';
          revoke.disabled = false;
        }
      });
      row.append(details, revoke);
      list.append(row);
    }
  }

  async function load() {
    if (loading) return;
    loading = true;
    create.disabled = true;
    refresh.disabled = true;
    list.replaceChildren();
    status.textContent = 'Loading tokens…';
    try {
      const data = await request('/api/tokens');
      if (!Array.isArray(data.items)) throw new Error('Could not load tokens.');
      render(data.items);
      status.textContent = '';
    } catch (error) {
      list.replaceChildren();
      status.textContent = error.message || 'Could not load tokens.';
    } finally {
      loading = false;
      create.disabled = creating;
      refresh.disabled = false;
    }
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (creating || loading) return;
    const name = form.elements.name.value.trim();
    if (!name) { form.elements.name.focus(); return; }
    creating = true;
    create.disabled = true;
    clearSecret();
    const started = generation;
    status.textContent = 'Creating token…';
    try {
      const data = await request('/api/tokens', {
        method: 'POST', body: JSON.stringify({ name, read_only: form.elements.access.value === 'read' }),
      });
      if (typeof data.token !== 'string' || !data.token.startsWith('ofd_') || !data.item?.id) {
        throw new Error('Could not display the new token. Refresh the list before trying again.');
      }
      if (started === generation && dialog.open) {
        secret.value = data.token;
        generatedId = data.item.id;
        result.hidden = false;
        secret.focus();
        secret.select();
      }
      await load();
      if (started !== generation || !dialog.open) {
        status.textContent = 'Token created while settings was closed. Revoke it and create a new token to copy its value.';
      }
    } catch (error) {
      status.textContent = error.message || 'Could not create token. Refresh the list before trying again.';
    } finally {
      creating = false;
      create.disabled = loading;
    }
  });

  pane.querySelector('[data-api-token-copy]').addEventListener('click', async () => {
    if (!secret.value) return;
    try {
      await navigator.clipboard.writeText(secret.value);
      status.textContent = 'Token copied.';
    } catch (_) {
      secret.focus();
      secret.select();
      status.textContent = 'Copy the selected token with your keyboard.';
    }
  });
  pane.querySelector('[data-api-token-dismiss]').addEventListener('click', clearSecret);
  refresh.addEventListener('click', load);
  const tab = document.querySelector('[data-settings-pane="api-tokens"]');
  function enter() {
    tab?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    load();
  }
  tab?.addEventListener('click', enter);
  document.addEventListener('click', (event) => {
    if (event.target.closest('[data-open-settings-dialog]') && pane.classList.contains('active')) load();
  });
  dialog.addEventListener('close', () => { generation += 1; clearSecret(); });
  window.addEventListener('pagehide', () => { generation += 1; clearSecret(); });
  if (dialog.open && pane.classList.contains('active')) enter();
})();
