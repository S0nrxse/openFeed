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
    if (!response.ok) throw new Error(`The server answered HTTP ${response.status}. Refresh the list before trying again.`);
    const data = await response.json();
    if (data.ok !== true) throw new Error('The server did not confirm that. Refresh the list before trying again.');
    return data;
  }

  function shortDate(value) {
    const date = new Date(value);
    if (!value || !Number.isFinite(date.getTime())) return '';
    return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }

  function ago(value) {
    const date = new Date(value);
    if (!value || !Number.isFinite(date.getTime())) return '';
    const minutes = Math.round((Date.now() - date.getTime()) / 60000);
    if (minutes < 1) return 'just now';
    if (minutes < 60) return `${minutes} min ago`;
    if (minutes < 1440) return `${Math.round(minutes / 60)} h ago`;
    return shortDate(value);
  }

  function renderEmpty() {
    const empty = document.createElement('p');
    empty.className = 'api-token-empty';
    empty.textContent = 'No API tokens yet. Create one above and give it to the script that needs it.';
    list.replaceChildren(empty);
  }

  function renderSkeleton() {
    list.replaceChildren();
    for (let index = 0; index < 2; index += 1) {
      const row = document.createElement('div');
      row.className = 'api-token-row';
      row.setAttribute('aria-hidden', 'true');
      row.innerHTML = '<div><span class="skeleton skeleton-line w-40"></span><span class="skeleton skeleton-line is-meta w-64"></span></div>';
      list.append(row);
    }
  }

  function render(items) {
    list.replaceChildren();
    if (!items.length) { renderEmpty(); return; }
    for (const item of items) {
      const label = item.name || 'API token';
      const row = document.createElement('div');
      row.className = 'api-token-row';
      const details = document.createElement('div');
      details.className = 'api-token-copy';
      const heading = document.createElement('div');
      heading.className = 'api-token-heading';
      const name = document.createElement('strong');
      name.textContent = label;
      const access = document.createElement('span');
      access.className = 'api-token-access';
      access.textContent = item.read_only ? 'Read only' : 'Read and write';
      heading.append(name, access);
      /* The prefix is the public half. The secret after it is never stored. */
      const id = document.createElement('code');
      id.textContent = `${item.prefix || 'ofd'}_…`;
      const meta = document.createElement('small');
      const lastUsed = ago(item.last_used_at);
      meta.textContent = `Created ${shortDate(item.created_at) || 'unknown'} · ${lastUsed ? `Last used ${lastUsed}` : 'Never used'}`;
      details.append(heading, id, meta);
      const revoke = document.createElement('button');
      revoke.type = 'button';
      revoke.className = 'btn btn-sm btn-danger';
      revoke.textContent = 'Revoke';
      revoke.setAttribute('aria-label', `Revoke ${label}`);
      revoke.addEventListener('click', async () => {
        const confirmed = await window.ofConfirm({
          title: `Revoke ${label}?`,
          body: `Anything using ${item.prefix || 'this token'} stops working at once. This cannot be undone.`,
          confirmLabel: 'Revoke',
          danger: true,
        });
        if (!confirmed) { revoke.focus(); return; }
        revoke.disabled = true;
        status.textContent = '';
        try {
          await request(`/api/tokens/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
          if (generatedId === item.id) clearSecret();
          row.remove();
          window.ofToast?.(`${label} revoked`, { kind: 'success' });
          if (!list.children.length) renderEmpty();
          refresh.focus();
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
    renderSkeleton();
    status.textContent = '';
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
    status.textContent = 'Creating token';
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
        form.elements.name.value = '';
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
      status.textContent = '';
      window.ofToast?.('Token copied', { kind: 'info' });
    } catch (_) {
      secret.focus();
      secret.select();
      status.textContent = 'Copy the selected token with your keyboard.';
    }
  });
  pane.querySelector('[data-api-token-dismiss]').addEventListener('click', () => {
    clearSecret();
    form.elements.name.focus();
  });
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
