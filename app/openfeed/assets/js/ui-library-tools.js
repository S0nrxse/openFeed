/* Library tools: select and bulk edit on History, Liked and playlists, playlist reorder,
   visibility and sort, History pause and clear, YouTube playlist import, undo for deleted
   playlists and collections, and Download again for a missing file. Server side:
   library_tools.py and the /api/history, /api/liked, /api/playlists and /api/library
   routes. Talks to the page only through data attributes, so it can load everywhere. */
(() => {
  'use strict';

  const UNDO_MS = 6000;
  const FLASH_KEY = 'openfeed.libraryFlash';
  const toast = (message, options) => window.ofToast?.(message, options) || { dismiss() {} };
  const plural = (count, noun) => `${count} ${noun}${count === 1 ? '' : 's'}`;
  const videos = (count) => plural(count, 'video');

  async function send(url, method = 'POST', body = undefined, keepalive = false) {
    const response = await fetch(url, {
      method,
      keepalive,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = {};
    try { data = await response.json(); } catch (error) { /* an empty answer is fine */ }
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    return data;
  }

  const announcer = document.createElement('div');
  announcer.className = 'sr-only';
  announcer.setAttribute('aria-live', 'polite');
  document.body.append(announcer);
  const announce = (text) => { announcer.textContent = ''; window.setTimeout(() => { announcer.textContent = text; }, 30); };

  /* ---- a message that survives a navigation (delete from a playlist's own page) ---- */
  function flash(entry) {
    try { window.sessionStorage.setItem(FLASH_KEY, JSON.stringify({ ...entry, at: Date.now() })); } catch (error) { /* no toast then */ }
  }

  function takeFlash() {
    try {
      const raw = window.sessionStorage.getItem(FLASH_KEY);
      window.sessionStorage.removeItem(FLASH_KEY);
      const entry = raw ? JSON.parse(raw) : null;
      return entry && Date.now() - entry.at < 15000 ? entry : null;
    } catch (error) {
      return null;
    }
  }

  async function restore(undoId, { back = '', onRestored = null, label = 'It' } = {}) {
    try {
      await send(`/api/library/restore/${encodeURIComponent(undoId)}`);
      if (onRestored) {
        onRestored();
        toast(`${label} restored`, { kind: 'success' });
      } else {
        window.location.href = back || window.location.href;
      }
    } catch (error) {
      toast(`Could not restore: ${error.message}`, { kind: 'error' });
    }
  }

  /* ---- counts after rows leave or come back ---- */
  function syncLists() {
    document.querySelectorAll('[data-day-group]').forEach((group) => {
      const count = group.querySelectorAll('[data-video-card]:not([hidden])').length;
      const label = group.querySelector('[data-day-count]');
      if (label) label.textContent = videos(count);
      group.hidden = !count;
    });
    document.querySelectorAll('.library-list').forEach((list) => {
      [...list.querySelectorAll(':scope > [data-video-card]')].forEach((row, index) => {
        const cell = row.querySelector('.row-index');
        if (cell) cell.textContent = String(index + 1);
      });
    });
  }

  async function refreshTotals() {
    if (!document.querySelector('main [data-video-card]')) {
      window.location.reload();
      return;
    }
    try {
      const html = await fetch(window.location.href).then((response) => response.text());
      const next = new DOMParser().parseFromString(html, 'text/html');
      ['.page-head .page-meta', '.playlist-meta'].forEach((selector) => {
        const current = document.querySelector(selector);
        const fresh = next.querySelector(selector);
        if (current && fresh && current.dataset.metaBase === undefined) current.textContent = fresh.textContent;
      });
    } catch (error) { /* A stale total is not worth interrupting anyone for. */ }
  }

  /* ---- removal held for an undo toast ----
     Rows are taken out of the document (a marker keeps their place), so the filter box
     cannot bring them back while the toast is up. One request commits the lot. */
  const held = new Set();

  function holdRemoval(rows, { commit, message, failure }) {
    rows = rows.filter((row) => row && row.isConnected);
    if (!rows.length) return;
    const parked = rows.map((row) => {
      const marker = document.createComment('held');
      row.before(marker);
      row.remove();
      return { row, marker };
    });
    syncLists();
    let timer = 0;
    let notice = null;
    const putBack = () => {
      parked.forEach(({ row, marker }) => { marker.replaceWith(row); });
      syncLists();
    };
    const entry = { done: false };
    const finish = async (leaving = false) => {
      if (entry.done) return;
      entry.done = true;
      window.clearTimeout(timer);
      held.delete(entry);
      notice?.dismiss();
      try {
        await commit(leaving);
        if (leaving) return;
        parked.forEach(({ marker }) => marker.remove());
        refreshTotals();
      } catch (error) {
        if (leaving) return;
        putBack();
        toast(failure, { kind: 'error' });
      }
    };
    entry.finish = finish;
    held.add(entry);
    timer = window.setTimeout(() => finish(), UNDO_MS);
    notice = toast(message, {
      actionLabel: 'Undo',
      duration: UNDO_MS,
      action: () => {
        if (entry.done) return;
        entry.done = true;
        window.clearTimeout(timer);
        held.delete(entry);
        putBack();
        announce('Restored');
      },
    });
  }
  window.addEventListener('pagehide', () => held.forEach((entry) => entry.finish(true)));

  const rowId = (row) => row.dataset.videoId || row.dataset.playlistRow || '';

  function removeRows(scope, rows) {
    const kind = scope?.dataset.selectKind;
    const ids = rows.map(rowId).filter(Boolean);
    if (!ids.length) return;
    const count = videos(ids.length);
    const where = {
      history: { url: '/api/history/remove', label: 'history' },
      liked: { url: '/api/liked/remove', label: 'Liked videos' },
      playlist: { url: `/api/playlists/${encodeURIComponent(scope.dataset.playlistId || '')}/items/remove`, label: 'the playlist' },
    }[kind];
    if (!where) return;
    holdRemoval(rows, {
      commit: (leaving) => send(where.url, 'POST', { youtube_video_ids: ids }, leaving),
      message: ids.length === 1 ? `Removed from ${where.label}` : `Removed ${count} from ${where.label}`,
      failure: `Could not remove ${ids.length === 1 ? 'that video' : 'those videos'} from ${where.label}`,
    });
  }

  /* ---- select mode ---- */
  let selecting = null;

  function rowsOf(scope) {
    return [...scope.querySelectorAll('.library-list > [data-video-card]')];
  }

  function checkOf(row) {
    return row.querySelector(':scope > .select-check input');
  }

  function selectedRows() {
    return selecting ? rowsOf(selecting.scope).filter((row) => checkOf(row)?.checked) : [];
  }

  function paintSelection() {
    if (!selecting) return;
    const { bar, scope } = selecting;
    const picked = selectedRows();
    const visible = rowsOf(scope).filter((row) => !row.hidden && !row.closest('[hidden]'));
    rowsOf(scope).forEach((row) => row.classList.toggle('is-selected', Boolean(checkOf(row)?.checked)));
    bar.querySelector('[data-select-count]').textContent = picked.length ? `${picked.length} selected` : 'Select videos';
    const all = bar.querySelector('[data-select-all]');
    const everything = visible.length && visible.every((row) => checkOf(row)?.checked);
    all.textContent = everything ? 'Deselect all' : 'Select all';
    all.disabled = !visible.length;
    bar.querySelectorAll('[data-select-action]').forEach((button) => { button.disabled = !picked.length; });
  }

  function buildBar(scope) {
    const kind = scope.dataset.selectKind;
    const readonly = scope.hasAttribute('data-select-readonly');
    const bar = document.createElement('div');
    bar.className = 'select-bar';
    bar.setAttribute('role', 'toolbar');
    bar.setAttribute('aria-label', 'Selected videos');
    const removeLabel = { history: 'Remove from history', liked: 'Remove from Liked', playlist: 'Remove from playlist' }[kind] || 'Remove';
    bar.innerHTML = `
      <button class="btn btn-ghost btn-icon" type="button" data-select-exit aria-label="Done selecting" title="Done (Esc)">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg></button>
      <span class="select-count" data-select-count aria-live="polite"></span>
      <button class="btn btn-sm btn-ghost" type="button" data-select-all></button>
      <span class="select-bar-actions">
        <button class="btn btn-sm" type="button" data-select-action="playlist">Add to playlist</button>
        ${readonly ? '' : `<button class="btn btn-sm btn-danger" type="button" data-select-action="remove">${removeLabel}</button>`}
      </span>`;
    return bar;
  }

  function enterSelect(scope, toggle) {
    if (selecting) exitSelect();
    const bar = buildBar(scope);
    const toolbar = scope.querySelector('.library-toolbar');
    if (toolbar) toolbar.after(bar); else scope.prepend(bar);
    rowsOf(scope).forEach((row) => {
      const title = row.querySelector('h2')?.textContent?.trim() || 'video';
      const label = document.createElement('label');
      label.className = 'select-check';
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.setAttribute('aria-label', `Select ${title}`);
      label.append(box);
      row.prepend(label);
    });
    scope.classList.add('is-selecting');
    toggle?.setAttribute('aria-pressed', 'true');
    selecting = { scope, bar, toggle, last: -1 };
    paintSelection();
    (rowsOf(scope).find((row) => !row.hidden) && checkOf(rowsOf(scope).find((row) => !row.hidden)))?.focus({ preventScroll: true });
    announce('Select mode. Choose videos, then an action.');
  }

  function exitSelect() {
    if (!selecting) return;
    const { scope, bar, toggle } = selecting;
    scope.classList.remove('is-selecting');
    scope.querySelectorAll('.select-check').forEach((label) => label.remove());
    scope.querySelectorAll('.is-selected').forEach((row) => row.classList.remove('is-selected'));
    bar.remove();
    toggle?.setAttribute('aria-pressed', 'false');
    selecting = null;
    toggle?.focus({ preventScroll: true });
  }

  /* Taps and clicks anywhere on a row pick it while selecting; links do not navigate. */
  document.addEventListener('click', (event) => {
    if (!selecting) return;
    const row = event.target.closest('[data-video-card]');
    if (!row || !selecting.scope.contains(row) || !row.parentElement?.matches('.library-list')) return;
    const box = checkOf(row);
    /* The checkbox and its label toggle themselves. */
    if (!box || event.target.closest('.select-check')) return;
    event.preventDefault();
    event.stopPropagation();
    box.checked = !box.checked;
    box.dispatchEvent(new Event('change', { bubbles: true }));
  }, true);

  document.addEventListener('change', (event) => {
    if (!selecting || !event.target.matches('.select-check input')) return;
    const rows = rowsOf(selecting.scope);
    const index = rows.indexOf(event.target.closest('[data-video-card]'));
    /* Shift extends from the last row picked, as in a file list. */
    if (selecting.shift && selecting.last >= 0 && index >= 0) {
      const [from, to] = [Math.min(selecting.last, index), Math.max(selecting.last, index)];
      rows.slice(from, to + 1).forEach((row) => { if (!row.hidden) checkOf(row).checked = event.target.checked; });
    }
    selecting.last = index;
    paintSelection();
  });
  document.addEventListener('pointerdown', (event) => { if (selecting) selecting.shift = event.shiftKey; }, true);
  document.addEventListener('keydown', (event) => {
    if (selecting) selecting.shift = event.shiftKey;
    if (event.key !== 'Escape' || !selecting) return;
    if (document.querySelector('dialog[open], .yt-menu[open]')) return;
    event.preventDefault();
    exitSelect();
  }, true);
  /* The filter box hides rows; the count and Select all follow it. */
  document.addEventListener('input', (event) => {
    if (selecting && event.target.matches?.('[data-filter-input]')) window.setTimeout(paintSelection, 0);
  });

  document.addEventListener('click', async (event) => {
    const toggle = event.target.closest('[data-select-toggle]');
    if (toggle) {
      event.preventDefault();
      const scope = toggle.closest('[data-select-scope]') || document.querySelector('[data-select-scope]');
      if (!scope) return;
      if (selecting && selecting.scope === scope) exitSelect(); else enterSelect(scope, toggle);
      return;
    }
    if (!selecting) return;
    if (event.target.closest('[data-select-exit]')) {
      exitSelect();
      return;
    }
    if (event.target.closest('[data-select-all]')) {
      const visible = rowsOf(selecting.scope).filter((row) => !row.hidden && !row.closest('[hidden]'));
      const everything = visible.every((row) => checkOf(row)?.checked);
      visible.forEach((row) => { checkOf(row).checked = !everything; });
      paintSelection();
      announce(everything ? 'Nothing selected' : `${visible.length} selected`);
      return;
    }
    const action = event.target.closest('[data-select-action]');
    if (!action) return;
    const rows = selectedRows();
    if (!rows.length) return;
    const { scope } = selecting;
    if (action.dataset.selectAction === 'remove') {
      exitSelect();
      removeRows(scope, rows);
    } else if (action.dataset.selectAction === 'playlist') {
      openBulkPicker(rows.map(rowId).filter(Boolean), scope.dataset.playlistId || '');
    }
  });

  /* ---- Add to playlist for a selection ---- */
  let picker = null;

  function buildPicker() {
    picker = document.createElement('dialog');
    picker.className = 'feed-dialog save-picker bulk-picker';
    picker.setAttribute('aria-labelledby', 'bulk-picker-title');
    picker.innerHTML = `
      <div class="dialog-head">
        <div><h2 id="bulk-picker-title">Save to playlist</h2><p class="save-picker-subtitle" data-bulk-subtitle></p></div>
        <button class="icon-button" type="button" data-close-dialog aria-label="Close">
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg></button>
      </div>
      <form class="save-picker-search" data-bulk-create>
        <input class="field-input" type="text" name="name" placeholder="New playlist name" aria-label="New playlist name" maxlength="120" autocomplete="off">
        <button class="btn btn-sm" type="submit">Create</button>
      </form>
      <div class="save-picker-list" data-bulk-list></div>`;
    document.body.append(picker);
  }

  async function saveTo(playlist, ids) {
    try {
      const data = await send(`/api/playlists/${encodeURIComponent(playlist.id)}/items/bulk`, 'POST', { youtube_video_ids: ids });
      picker.close();
      exitSelect();
      const added = Number(data.added || 0);
      const already = ids.length - added;
      toast(added
        ? `Saved ${videos(added)} to ${playlist.name}${already ? ` (${already} already there)` : ''}`
        : `Already in ${playlist.name}`, { kind: 'success' });
    } catch (error) {
      toast(`Could not save to ${playlist.name}`, { kind: 'error', duration: 5000 });
    }
  }

  async function openBulkPicker(ids, currentPlaylist) {
    if (!picker) buildPicker();
    const list = picker.querySelector('[data-bulk-list]');
    picker.querySelector('[data-bulk-subtitle]').textContent = `${videos(ids.length)} selected`;
    list.innerHTML = '<span class="sr-only">Loading playlists</span>' + '<span class="skeleton save-row-skeleton"></span>'.repeat(3);
    const form = picker.querySelector('[data-bulk-create]');
    form.reset();
    form.onsubmit = async (event) => {
      event.preventDefault();
      const name = String(new FormData(form).get('name') || '').trim();
      if (!name) return;
      try {
        const data = await send('/api/playlists', 'POST', { name, description: null });
        await saveTo({ id: data.playlist.id, name }, ids);
      } catch (error) {
        toast(`Could not create ${name}`, { kind: 'error', duration: 5000 });
      }
    };
    picker.showModal();
    try {
      const data = await fetch('/api/playlists').then((response) => {
        if (!response.ok) throw new Error('load failed');
        return response.json();
      });
      const items = (data.items || []).filter((item) => String(item.id) !== currentPlaylist);
      list.replaceChildren();
      if (!items.length) {
        list.innerHTML = '<p class="picker-empty">No other playlists yet. Name one above to create it.</p>';
        return;
      }
      items.forEach((item) => {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'save-row';
        const cover = document.createElement('span');
        cover.className = 'save-cover';
        if (item.cover_url) {
          const image = document.createElement('img');
          image.src = item.cover_url;
          image.alt = '';
          image.loading = 'lazy';
          image.addEventListener('error', () => image.remove());
          cover.append(image);
        }
        const copy = document.createElement('span');
        copy.className = 'save-copy';
        const name = document.createElement('span');
        name.textContent = item.name || 'Untitled playlist';
        const count = document.createElement('small');
        count.textContent = videos(Number(item.video_count || 0));
        copy.append(name, count);
        row.append(cover, copy);
        row.addEventListener('click', () => saveTo({ id: item.id, name: item.name || 'Untitled playlist' }, ids));
        list.append(row);
      });
    } catch (error) {
      list.innerHTML = '<p class="picker-empty">Could not load your playlists.</p>';
    }
  }

  /* ---- one Liked video ---- */
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-remove-liked]');
    if (!button) return;
    event.preventDefault();
    button.closest('.yt-menu')?.removeAttribute('open');
    const row = button.closest('[data-video-card]');
    const id = button.dataset.removeLiked;
    holdRemoval([row], {
      commit: (leaving) => send('/api/liked/remove', 'POST', { youtube_video_ids: [id] }, leaving),
      message: 'Removed from Liked videos',
      failure: 'Could not remove that video from Liked videos',
    });
  });

  /* ---- playlist order ---- */
  function reorderList() {
    return document.querySelector('[data-reorder-list]');
  }

  function applyOrder(list, order) {
    const rows = new Map([...list.querySelectorAll(':scope > [data-video-card]')].map((row) => [rowId(row), row]));
    order.forEach((id) => { const row = rows.get(id); if (row) list.append(row); });
    /* Anything not in the answer (a row past the page) keeps its place at the end. */
    syncLists();
  }

  async function move(row, payload, focusTarget) {
    const list = row?.closest('[data-reorder-list]');
    if (!list) return;
    try {
      const data = await send(`/api/playlists/${encodeURIComponent(list.dataset.playlistId)}/items/move`, 'POST', { youtube_video_id: rowId(row), ...payload });
      applyOrder(list, data.order || []);
      const rows = [...list.querySelectorAll(':scope > [data-video-card]')];
      announce(`Moved to position ${rows.indexOf(row) + 1} of ${rows.length}`);
      focusTarget?.focus({ preventScroll: false });
      return true;
    } catch (error) {
      toast('Could not move that video', { kind: 'error' });
      return false;
    }
  }

  const MOVE_WORDS = { top: 'Moved to top', bottom: 'Moved to bottom', up: 'Moved up', down: 'Moved down' };
  document.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-playlist-move]');
    if (!button) return;
    event.preventDefault();
    const menu = button.closest('.yt-menu');
    menu?.removeAttribute('open');
    const row = button.closest('[data-video-card]');
    if (await move(row, { move: button.dataset.playlistMove }, menu?.querySelector('summary'))) {
      toast(MOVE_WORDS[button.dataset.playlistMove] || 'Moved');
    }
  });

  /* Arrow keys on a focused handle move the row; Home and End go to the ends. */
  document.addEventListener('keydown', (event) => {
    const handle = event.target.closest?.('[data-drag-handle]');
    if (!handle) return;
    const step = { ArrowUp: 'up', ArrowDown: 'down', Home: 'top', End: 'bottom' }[event.key];
    if (!step) return;
    event.preventDefault();
    move(handle.closest('[data-video-card]'), { move: step }, handle);
  });

  /* Dragging by the handle: the row follows the pointer through the list, and the drop
     sends the index it landed at. Pointer events cover mouse, pen and touch alike. */
  let drag = null;
  document.addEventListener('pointerdown', (event) => {
    const handle = event.target.closest('[data-drag-handle]');
    if (!handle || event.button !== 0) return;
    const row = handle.closest('[data-video-card]');
    const list = row?.closest('[data-reorder-list]');
    if (!list) return;
    event.preventDefault();
    handle.setPointerCapture?.(event.pointerId);
    const rows = [...list.querySelectorAll(':scope > [data-video-card]')];
    drag = { handle, row, list, startY: event.clientY, start: rows.indexOf(row), moved: false };
  });

  document.addEventListener('pointermove', (event) => {
    if (!drag) return;
    if (!drag.moved && Math.abs(event.clientY - drag.startY) < 4) return;
    drag.moved = true;
    drag.row.classList.add('is-dragging');
    drag.list.classList.add('is-reordering');
    const siblings = [...drag.list.querySelectorAll(':scope > [data-video-card]:not([hidden])')].filter((row) => row !== drag.row);
    const after = siblings.find((row) => {
      const box = row.getBoundingClientRect();
      return event.clientY < box.top + box.height / 2;
    });
    if (after) {
      if (drag.row.nextElementSibling !== after) after.before(drag.row);
    } else if (siblings.length) {
      siblings[siblings.length - 1].after(drag.row);
    }
    /* Keep the edge of the list reachable without letting go. */
    if (event.clientY < 90) window.scrollBy(0, -12);
    else if (event.clientY > window.innerHeight - 60) window.scrollBy(0, 12);
  });

  async function endDrag() {
    if (!drag) return;
    const { row, list, start, moved, handle } = drag;
    drag = null;
    row.classList.remove('is-dragging');
    list.classList.remove('is-reordering');
    if (!moved) return;
    const index = [...list.querySelectorAll(':scope > [data-video-card]')].indexOf(row);
    syncLists();
    if (index === start) return;
    if (!await move(row, { index }, handle)) window.location.reload();
  }
  document.addEventListener('pointerup', endDrag);
  document.addEventListener('pointercancel', endDrag);

  /* ---- playlist visibility and sort ---- */
  document.addEventListener('click', async (event) => {
    const choice = event.target.closest('[data-playlist-visibility][data-playlist-id]');
    if (choice) {
      event.preventDefault();
      const menu = choice.closest('.yt-menu');
      menu?.removeAttribute('open');
      const value = choice.dataset.playlistVisibility;
      try {
        await send(`/api/playlists/${encodeURIComponent(choice.dataset.playlistId)}/settings`, 'POST', { visibility: value });
        menu?.querySelectorAll('[data-playlist-visibility]').forEach((row) => {
          const current = row === choice;
          row.toggleAttribute('aria-current', current);
          const tick = row.querySelector('.menu-trailing');
          if (tick) tick.textContent = current ? '✓' : '';
        });
        const summary = menu?.querySelector('[data-visibility-summary]');
        const label = choice.querySelector('.menu-label')?.textContent || value;
        if (summary) {
          summary.querySelector('span').textContent = label;
          summary.setAttribute('aria-label', `Visibility: ${label}`);
          const icon = choice.querySelector('.menu-icon .icon');
          if (icon) summary.querySelector('.icon').replaceWith(icon.cloneNode(true));
        }
        document.querySelectorAll('[data-playlist-rename]').forEach((button) => { button.dataset.playlistVisibility = value; });
        toast(`Visibility set to ${label}`, { kind: 'success' });
      } catch (error) {
        toast('Could not change the visibility', { kind: 'error' });
      }
      return;
    }
    const sort = event.target.closest('[data-playlist-sort]');
    if (sort) {
      /* The owner's sort is the playlist's own order from now on, Play all included. */
      event.preventDefault();
      try {
        await send(`/api/playlists/${encodeURIComponent(sort.dataset.playlistId)}/settings`, 'POST', { sort: sort.dataset.playlistSort });
        window.location.href = window.location.pathname;
      } catch (error) {
        window.location.href = sort.getAttribute('href');
      }
    }
  });

  /* ---- History: pause and clear ---- */
  document.addEventListener('click', async (event) => {
    const pause = event.target.closest('[data-history-pause]');
    if (pause) {
      event.preventDefault();
      const pausing = pause.dataset.historyPause === 'on';
      if (pausing && !await window.ofConfirm({
        title: 'Pause watch history?',
        body: 'Videos you watch on this profile won’t be added to History or keep a resume point, and they won’t shape your recommendations. What is already in History stays.',
        confirmLabel: 'Pause',
      })) return;
      pause.disabled = true;
      try {
        await send('/api/history/pause', 'POST', { paused: pausing });
        flash({ message: pausing ? 'Watch history paused' : 'Watch history is on', kind: 'success' });
        window.location.reload();
      } catch (error) {
        pause.disabled = false;
        toast('Could not change watch history', { kind: 'error' });
      }
      return;
    }
    const clear = event.target.closest('[data-history-clear]');
    if (clear) {
      event.preventDefault();
      if (!await window.ofConfirm({
        title: 'Clear all watch history?',
        body: 'Everything this profile watched leaves History, along with resume points and watch time, and recommendations stop drawing on it. Likes, playlists and an imported Takeout history stay. This can’t be undone.',
        confirmLabel: 'Clear watch history',
        danger: 'filled',
      })) return;
      clear.disabled = true;
      try {
        await send('/api/history/clear');
        flash({ message: 'Watch history cleared', kind: 'success' });
        window.location.reload();
      } catch (error) {
        clear.disabled = false;
        toast('Could not clear watch history', { kind: 'error' });
      }
    }
  });

  /* ---- delete a playlist or collection, with Undo ---- */
  window.ofDeleteWithUndo = async (button, { url, card, message, failure }) => {
    button.disabled = true;
    let data;
    try {
      data = await send(url, 'DELETE');
    } catch (error) {
      button.disabled = false;
      toast(failure, { kind: 'error' });
      return;
    }
    const noun = message.split(' ')[0];
    if (button.dataset.redirect) {
      flash({ message, undo: data.undo_id, back: window.location.pathname + window.location.search, noun });
      window.location.href = button.dataset.redirect;
      return;
    }
    button.disabled = false;
    const tile = button.closest(card) || document.querySelector(`${card}[${card.slice(1, -1)}="${CSS.escape(button.dataset.deletePlaylist || button.dataset.deleteCollection || '')}"]`);
    let marker = null;
    if (tile) {
      marker = document.createComment('deleted');
      tile.before(marker);
      tile.remove();
    }
    toast(message, {
      actionLabel: data.undo_id ? 'Undo' : undefined,
      duration: UNDO_MS,
      action: data.undo_id ? () => restore(data.undo_id, {
        label: noun,
        onRestored: marker && tile ? () => { marker.replaceWith(tile); } : null,
      }) : undefined,
    });
  };

  /* ---- Download again ---- */
  document.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-redownload]');
    if (!button) return;
    event.preventDefault();
    button.closest('.yt-menu')?.removeAttribute('open');
    const id = button.dataset.redownload;
    const triggers = document.querySelectorAll(`[data-redownload="${CSS.escape(id)}"]`);
    triggers.forEach((item) => { item.disabled = true; });
    try {
      await send(`/api/download/${encodeURIComponent(id)}/jobs`, 'POST', {
        height: Number(button.dataset.height || 0),
        container: button.dataset.container || 'mp4',
        title: button.dataset.videoTitle || '',
        needs_merge: false,
        destination: 'server',
      });
      triggers.forEach((item) => {
        const label = item.querySelector('span, .menu-label');
        if (label) label.textContent = 'Queued';
      });
      toast('Downloading again', { kind: 'success' });
      /* Wakes the downloads page's poller, which lists the job under Downloading. */
      document.dispatchEvent(new Event('visibilitychange'));
    } catch (error) {
      triggers.forEach((item) => { item.disabled = false; });
      toast(`Could not download again: ${error.message}`, { kind: 'error' });
    }
  });

  /* ---- Import a YouTube playlist ---- */
  const importDialog = document.getElementById('playlist-import-dialog');
  const YOUTUBE_LIST = /(youtube\.com|youtu\.be).*[?&]list=|^(PL|UU|OL|FL)[A-Za-z0-9_-]{10,}$/;
  let importTimer = 0;
  let importDone = false;

  function importParts() {
    const form = importDialog.querySelector('[data-playlist-import-form]');
    return {
      form,
      progress: form.querySelector('[data-pl-import-progress]'),
      bar: form.querySelector('[data-pl-import-bar]'),
      text: form.querySelector('[data-pl-import-text]'),
      status: form.querySelector('[data-pl-import-status]'),
      submit: form.querySelector('[data-pl-import-submit]'),
      open: form.querySelector('[data-pl-import-open]'),
    };
  }

  function openImport(url = '') {
    if (!importDialog) return;
    window.clearTimeout(importTimer);
    importDone = false;
    const parts = importParts();
    parts.form.reset();
    parts.form.querySelector('[name="url"]').value = url;
    parts.progress.hidden = true;
    parts.status.textContent = '';
    parts.submit.hidden = false;
    parts.submit.disabled = false;
    parts.open.hidden = true;
    parts.form.querySelectorAll('input, select').forEach((field) => { field.disabled = false; });
    document.getElementById('playlist-edit-dialog')?.close();
    importDialog.showModal();
    parts.form.querySelector(url ? '[name="name"]' : '[name="url"]').focus();
  }
  window.ofOpenPlaylistImport = openImport;

  function paintJob(job) {
    const parts = importParts();
    parts.progress.hidden = false;
    const fill = parts.bar.querySelector('span');
    const indeterminate = !job.total || job.status === 'queued' || job.status === 'saving';
    parts.bar.classList.toggle('is-indeterminate', indeterminate);
    fill.style.width = indeterminate ? '' : `${Math.min(100, Math.round((job.fetched / job.total) * 100))}%`;
    const number = (value) => Number(value || 0).toLocaleString();
    if (job.status === 'queued') parts.text.textContent = 'Starting…';
    else if (job.status === 'fetching') parts.text.textContent = job.total ? `Reading the playlist: ${number(job.fetched)} of ${number(job.total)} videos` : 'Reading the playlist…';
    else if (job.status === 'saving') parts.text.textContent = `Saving ${number(job.fetched)} videos${job.title ? ` from “${job.title}”` : ''}…`;
  }

  async function pollImport(id) {
    const parts = importParts();
    try {
      const data = await fetch(`/api/playlists/import/${encodeURIComponent(id)}`).then((response) => {
        if (!response.ok) throw new Error('The import was lost. Try again.');
        return response.json();
      });
      const { job } = data;
      if (job.status === 'done') {
        importDone = true;
        parts.bar.classList.remove('is-indeterminate');
        parts.bar.querySelector('span').style.width = '100%';
        const skipped = Number(job.skipped || 0);
        parts.text.textContent = `Imported ${videos(Number(job.added || 0))} into “${job.name}”.${skipped ? ` ${skipped} could not be read (deleted, private or blocked).` : ''}`;
        parts.submit.hidden = true;
        parts.open.href = `/playlist/${encodeURIComponent(job.playlist_id)}`;
        parts.open.hidden = false;
        parts.open.focus();
        toast(`Imported “${job.name}”`, { kind: 'success' });
        return;
      }
      if (job.status === 'error') {
        parts.progress.hidden = true;
        parts.status.textContent = job.error || 'The import failed.';
        parts.submit.disabled = false;
        parts.form.querySelectorAll('input, select').forEach((field) => { field.disabled = false; });
        return;
      }
      paintJob(job);
      importTimer = window.setTimeout(() => pollImport(id), 900);
    } catch (error) {
      parts.status.textContent = error.message;
      parts.submit.disabled = false;
    }
  }

  if (importDialog) {
    const parts = importParts();
    parts.form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const data = new FormData(parts.form);
      parts.status.textContent = '';
      parts.submit.disabled = true;
      try {
        const answer = await send('/api/playlists/import', 'POST', {
          url: String(data.get('url') || '').trim(),
          name: String(data.get('name') || '').trim(),
          visibility: String(data.get('visibility') || 'private'),
        });
        parts.form.querySelectorAll('input, select').forEach((field) => { field.disabled = true; });
        paintJob(answer.job);
        pollImport(answer.job.id);
      } catch (error) {
        parts.status.textContent = error.message;
        parts.submit.disabled = false;
      }
    });
    /* Closing after a finished import shows the new playlist on the Playlists page. */
    importDialog.addEventListener('close', () => {
      window.clearTimeout(importTimer);
      if (importDone && window.location.pathname === '/playlists') window.location.reload();
    });
  }

  document.addEventListener('click', (event) => {
    const opener = event.target.closest('[data-open-playlist-import]');
    if (!opener) return;
    event.preventDefault();
    const name = document.querySelector('#playlist-edit-form [name="name"]');
    const pasted = name && YOUTUBE_LIST.test(name.value.trim()) ? name.value.trim() : '';
    openImport(pasted);
  });

  /* A YouTube playlist link pasted as a new playlist's name is an import. */
  document.querySelector('#playlist-edit-form [name="name"]')?.addEventListener('input', (event) => {
    const value = event.target.value.trim();
    const alt = document.querySelector('[data-playlist-import-alt]');
    if (!alt || alt.hidden || !YOUTUBE_LIST.test(value)) return;
    openImport(value);
  });

  /* ---- on load: a message carried over from the page before ---- */
  const carried = takeFlash();
  if (carried) {
    window.setTimeout(() => {
      toast(carried.message, carried.undo ? {
        actionLabel: 'Undo',
        duration: UNDO_MS,
        action: () => restore(carried.undo, { back: carried.back }),
      } : { kind: carried.kind || 'info' });
    }, 0);
  }
})();
