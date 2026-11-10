/* Channel tools: a YouTube playlist opened in the app (Save to library, Show more), the
   channel Live tab's Show more, select mode on the Channels page, and on Shorts the
   Subscribe pill and "Not interested". Talks to the page only through data attributes,
   so it can load on every page. The shell's own handlers do subscribing and the
   long-form "Not interested"; this file only adds what they do not. */
(() => {
  'use strict';

  const toast = (message, options) => window.ofToast?.(message, options);
  const plural = (count, noun) => `${count} ${noun}${count === 1 ? '' : 's'}`;
  const postJson = (url, body) => fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });
  const closeMenu = (node) => node?.closest('.yt-menu')?.removeAttribute('open');

  /* ---- a YouTube playlist in the app ---- */
  async function saveRemotePlaylist(button) {
    const id = button.dataset.saveRemotePlaylist;
    button.disabled = true;
    try {
      const response = await postJson(`/api/remote-playlists/${encodeURIComponent(id)}/save`);
      if (!response.ok) throw new Error('save failed');
      const data = await response.json();
      const link = document.createElement('a');
      link.className = 'btn';
      link.href = data.href;
      link.innerHTML = `${CHECK_SVG}<span>Saved to library</span>`;
      button.replaceWith(link);
      toast(`Saved ${plural(data.added || 0, 'video')} to "${data.name}"`, {
        kind: 'success', actionLabel: 'View', action: () => { window.location.href = data.href; }
      });
    } catch (error) {
      button.disabled = false;
      toast('Could not save this playlist', { kind: 'error' });
    }
  }

  async function appendPage(button, url, target, onData) {
    button.disabled = true;
    const label = button.textContent;
    button.textContent = 'Loading…';
    try {
      const response = await fetch(url);
      if (!response.ok) throw new Error('load failed');
      const data = await response.json();
      target?.insertAdjacentHTML('beforeend', data.html || '');
      onData(data);
    } catch (error) {
      toast('Could not load more', { kind: 'error' });
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }

  function morePlaylistRows(button) {
    const id = button.dataset.remotePlaylistMore;
    const offset = Number(button.dataset.offset || 0);
    appendPage(button, `/api/remote-playlists/${encodeURIComponent(id)}/rows?offset=${offset}`,
      document.querySelector('[data-remote-playlist-list]'), (data) => {
        button.dataset.offset = String(data.next_offset);
        if (!data.more || !data.count) button.closest('[data-remote-playlist-more-wrap]')?.remove();
      });
  }

  function moreLiveCards(button) {
    const id = button.dataset.liveMore;
    const token = button.dataset.continuation || '';
    appendPage(button, `/api/channels/${encodeURIComponent(id)}/streams/html?continuation=${encodeURIComponent(token)}`,
      document.querySelector('[data-live-grid]'), (data) => {
        button.dataset.continuation = data.continuation || '';
        if (!data.continuation || !data.count) button.closest('[data-live-more-wrap]')?.remove();
      });
  }

  /* ---- Channels page: select mode ---- */
  const bar = () => document.querySelector('[data-bulk-bar]');
  const rows = () => [...document.querySelectorAll('.channel-grid [data-subscription-card]')];
  const visibleRows = () => rows().filter((row) => !row.hidden && row.offsetParent !== null);
  const selectedRows = () => rows().filter((row) => row.querySelector('[data-bulk-check]')?.checked);
  const selecting = () => document.querySelector('.channel-grid')?.classList.contains('is-selecting');

  function syncBar() {
    const panel = bar();
    if (!panel) return;
    const count = selectedRows().length;
    panel.querySelector('[data-bulk-count]').textContent = `${count} selected`;
    panel.toggleAttribute('data-empty', count === 0);
    panel.querySelectorAll('[data-bulk-needs]').forEach((control) => {
      if (control.tagName === 'BUTTON') control.disabled = count === 0;
      else control.setAttribute('aria-disabled', count === 0 ? 'true' : 'false');
    });
    const visible = visibleRows();
    const all = panel.querySelector('[data-bulk-all]');
    const chosen = visible.filter((row) => row.querySelector('[data-bulk-check]')?.checked).length;
    all.checked = visible.length > 0 && chosen === visible.length;
    all.indeterminate = chosen > 0 && chosen < visible.length;
  }

  function setSelecting(on) {
    const grid = document.querySelector('.channel-grid');
    const toggle = document.querySelector('[data-bulk-toggle]');
    const panel = bar();
    if (!grid || !panel) return;
    grid.classList.toggle('is-selecting', on);
    panel.hidden = !on;
    toggle?.setAttribute('aria-pressed', on ? 'true' : 'false');
    if (toggle) toggle.hidden = on;
    rows().forEach((row) => {
      let box = row.querySelector('.bulk-check');
      if (on && !box) {
        const title = row.querySelector('.channel-row-copy h2')?.textContent?.trim() || 'channel';
        box = document.createElement('label');
        box.className = 'bulk-check';
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.dataset.bulkCheck = '';
        input.setAttribute('aria-label', `Select ${title}`);
        box.appendChild(input);
        row.prepend(box);
      } else if (!on && box) {
        box.remove();
        row.classList.remove('is-selected');
      }
    });
    syncBar();
    if (on) panel.querySelector('[data-bulk-all]')?.focus();
    else toggle?.focus();
  }

  function setRowChecked(row, checked) {
    const input = row.querySelector('[data-bulk-check]');
    if (!input) return;
    input.checked = checked;
    row.classList.toggle('is-selected', checked);
  }

  function updateTotal(removed) {
    const head = document.querySelector('[data-subscription-total]');
    if (!head) return;
    const next = Math.max(0, Number(head.dataset.subscriptionTotal || '0') - removed);
    head.dataset.subscriptionTotal = String(next);
    const meta = head.querySelector('.page-meta');
    if (meta) meta.textContent = plural(next, 'channel');
  }

  async function refreshSidebar() {
    const sidebar = document.querySelector('.desktop-sidebar');
    if (!sidebar) return;
    try {
      const response = await fetch('/api/sidebar/subscriptions', { headers: { Accept: 'text/html' } });
      if (!response.ok) return;
      const markup = (await response.text()).trim();
      const current = sidebar.querySelector('[data-subscription-nav]');
      if (!markup) { current?.remove(); return; }
      const parsed = document.createRange().createContextualFragment(markup).firstElementChild;
      if (parsed && current) current.replaceWith(parsed);
    } catch (error) { /* the old list stays */ }
  }

  function paintBell(row, level) {
    const trigger = row.querySelector('[data-notify-trigger]');
    if (!trigger) return;
    try {
      trigger.innerHTML = `${NOTIFY_ICONS[level]}${CHEVRON_ICON}`;
      trigger.setAttribute('aria-label', `Notifications: ${NOTIFY_LABELS[level]}`);
      trigger.title = `Notifications: ${NOTIFY_LABELS[level]}`;
    } catch (error) { /* the shell's icon table is not on this page */ }
    trigger.classList.toggle('is-muted', level === 'none');
    row.querySelectorAll('[data-notify-level]').forEach((choice) => {
      const tick = choice.querySelector('.menu-trailing');
      if (tick) tick.textContent = choice.dataset.notifyLevel === level ? '✓' : '';
    });
  }

  async function runBulk(control) {
    const action = control.dataset.bulkAction;
    const chosen = selectedRows();
    closeMenu(control);
    if (!chosen.length) return;
    const ids = chosen.map((row) => row.dataset.subscriptionCard).filter(Boolean);
    const body = { action, subscription_ids: ids };
    if (action === 'collection') body.collection_id = control.dataset.collectionId || '';
    if (action === 'notify') body.level = control.dataset.level || 'all';
    if (action === 'unsubscribe') {
      const ok = await window.ofConfirm?.({
        title: `Unsubscribe from ${plural(chosen.length, 'channel')}?`,
        body: 'Their videos and your watch history stay in OpenFeed.',
        confirmLabel: 'Unsubscribe',
        danger: true
      });
      if (!ok) return;
    }
    try {
      const response = await postJson('/api/subscriptions/bulk', body);
      if (!response.ok) throw new Error('bulk failed');
      const data = await response.json();
      const applied = new Set(data.applied || []);
      const done = chosen.filter((row) => applied.has(row.dataset.subscriptionCard));
      if (action === 'unsubscribe') {
        done.forEach((row) => row.remove());
        updateTotal(done.length);
        refreshSidebar();
        toast(`Unsubscribed from ${plural(done.length, 'channel')}`, { kind: 'success' });
        if (!rows().length) window.location.reload();
      } else if (action === 'collection') {
        const name = control.querySelector('.menu-label')?.textContent || 'the collection';
        toast(`Added ${plural(done.length, 'channel')} to ${name}`, { kind: 'success' });
      } else if (action === 'notify') {
        done.forEach((row) => paintBell(row, body.level));
        toast(`Notifications ${body.level === 'none' ? 'off' : 'on'} for ${plural(done.length, 'channel')}`, { kind: 'success' });
      } else {
        const skipped = (data.skipped || []).length;
        const note = skipped ? ` (${plural(skipped, 'feed')} without a YouTube channel left as is)` : '';
        toast(`Won't recommend ${plural(done.length, 'channel')}${note}`, {
          kind: 'success',
          actionLabel: 'Undo',
          action: async () => {
            const undo = await postJson('/api/subscriptions/bulk', { action: 'unmute', subscription_ids: [...applied] }).catch(() => null);
            toast(undo?.ok ? 'Recommending them again' : 'Could not undo', { kind: undo?.ok ? 'info' : 'error' });
          }
        });
      }
      syncBar();
    } catch (error) {
      toast('Could not change those channels', { kind: 'error' });
    }
  }

  /* ---- Shorts: Not interested, and one subscribe state per channel ---- */
  async function shortNotInterested(button) {
    const slide = button.closest('[data-shorts-slide]');
    const stage = slide?.closest('[data-shorts-stage]');
    const videoId = button.dataset.videoId;
    closeMenu(button);
    if (!slide || !stage || !videoId) return;
    try {
      const response = await postJson('/api/feedback', { youtube_video_id: videoId, event_type: 'hide', payload: { source: 'shorts_viewer' } });
      if (!response.ok) throw new Error('feedback failed');
    } catch (error) {
      toast('Could not save', { kind: 'error' });
      return;
    }
    const list = [...stage.querySelectorAll('[data-shorts-slide]')];
    const index = list.indexOf(slide);
    const next = slide.nextElementSibling;
    slide.querySelector('[data-shorts-frame] iframe')?.remove();
    slide.classList.remove('is-playing');
    slide.remove();
    /* The feed pages by offset over Shorts you have not hidden, so one fewer now. */
    stage.dataset.nextOffset = String(Math.max(0, Number(stage.dataset.nextOffset || 0) - 1));
    stage.dispatchEvent(new Event('scroll'));
    toast('Removed from recommendations', {
      actionLabel: 'Undo',
      action: async () => {
        try {
          const undo = await postJson('/api/feedback', { youtube_video_id: videoId, event_type: 'undo_hide', payload: { source: 'undo_toast' } });
          if (!undo.ok) throw new Error('undo failed');
          if (next && next.isConnected) stage.insertBefore(slide, next); else stage.appendChild(slide);
          stage.dataset.nextOffset = String(Number(stage.dataset.nextOffset || 0) + 1);
          stage.scrollTop = Math.max(0, index) * stage.clientHeight;
          stage.dispatchEvent(new Event('scroll'));
          toast('Restored');
        } catch (error) {
          toast('Could not undo', { kind: 'error' });
        }
      }
    });
  }

  function syncSubscribePills(changed) {
    const subscribed = changed.classList.contains('subscribed');
    const channelId = changed.dataset.channelId;
    if (!channelId) return;
    document.querySelectorAll('[data-shorts-subscribe]').forEach((pill) => {
      if (pill === changed || pill.dataset.channelId !== channelId) return;
      if (pill.classList.contains('subscribed') === subscribed) return;
      pill.classList.toggle('subscribed', subscribed);
      pill.toggleAttribute('data-unsubscribe-feed', subscribed);
      pill.toggleAttribute('data-subscribe-feed', !subscribed);
      const title = pill.dataset.title || 'channel';
      pill.setAttribute('aria-label', `${subscribed ? 'Unsubscribe from' : 'Subscribe to'} ${title}`);
      const label = pill.querySelector('span');
      if (label) label.textContent = subscribed ? 'Subscribed' : 'Subscribe';
    });
  }

  const CHECK_SVG = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>';

  document.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const save = target.closest('[data-save-remote-playlist]');
    if (save) { saveRemotePlaylist(save); return; }
    const morePlaylist = target.closest('[data-remote-playlist-more]');
    if (morePlaylist) { morePlaylistRows(morePlaylist); return; }
    const moreLive = target.closest('[data-live-more]');
    if (moreLive) { moreLiveCards(moreLive); return; }
    const notInterested = target.closest('[data-shorts-not-interested]');
    if (notInterested) { shortNotInterested(notInterested); return; }

    if (target.closest('[data-bulk-toggle]')) { setSelecting(true); return; }
    if (target.closest('[data-bulk-done]')) { setSelecting(false); return; }
    const needs = target.closest('summary[data-bulk-needs]');
    if (needs && needs.getAttribute('aria-disabled') === 'true') { event.preventDefault(); return; }
    const action = target.closest('[data-bulk-action]');
    if (action) { event.preventDefault(); runBulk(action); return; }
    /* In select mode a press on the row selects it rather than opening the channel. */
    if (selecting()) {
      const row = target.closest('.channel-grid [data-subscription-card]');
      if (row && !target.closest('.channel-row-actions, .bulk-check')) {
        event.preventDefault();
        const input = row.querySelector('[data-bulk-check]');
        setRowChecked(row, !input?.checked);
        syncBar();
      }
    }
  });

  document.addEventListener('change', (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (target.matches('[data-bulk-check]')) {
      target.closest('[data-subscription-card]')?.classList.toggle('is-selected', target.checked);
      syncBar();
    } else if (target.matches('[data-bulk-all]')) {
      visibleRows().forEach((row) => setRowChecked(row, target.checked));
      syncBar();
    }
  });

  /* The filter box hides rows; Select all follows what is visible. */
  document.addEventListener('input', (event) => {
    if (event.target instanceof Element && event.target.matches('[data-filter-input]') && selecting()) {
      window.setTimeout(syncBar, 0);
    }
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !selecting()) return;
    if (document.querySelector('dialog[open], .yt-menu[open]')) return;
    setSelecting(false);
  });

  const stage = document.querySelector('[data-shorts-stage]');
  if (stage && 'MutationObserver' in window) {
    new MutationObserver((records) => {
      records.forEach((record) => {
        if (record.target instanceof Element && record.target.matches('[data-shorts-subscribe]')) syncSubscribePills(record.target);
      });
    }).observe(stage, { subtree: true, attributes: true, attributeFilter: ['class'] });
  }
})();
