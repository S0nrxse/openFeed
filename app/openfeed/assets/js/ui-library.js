/* Library surface behaviour: undoable row removal, the filtered empty state, the save
   picker and the downloads page. Runs after ui-kit.js in the shell bundle and talks to the
   page only through data attributes, so it can load on every page. */
(() => {
  'use strict';

  const UNDO_MS = 6000;
  const toast = (message, options) => window.ofToast(message, options);
  const plural = (count, noun) => `${count} ${noun}${count === 1 ? '' : 's'}`;

  function refilter() {
    document.querySelectorAll('[data-filter-input]').forEach((input) => {
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
  }

  /* ---- counts ---- */
  function syncGroups() {
    document.querySelectorAll('[data-day-group]').forEach((group) => {
      const count = group.querySelectorAll('[data-video-card]:not([hidden])').length;
      const label = group.querySelector('[data-day-count]');
      if (label) label.textContent = plural(count, 'video');
      group.hidden = !count;
    });
    document.querySelectorAll('.row-index').forEach((index) => {
      const list = index.closest('.library-list');
      const rows = [...list.querySelectorAll('[data-video-card]')].filter((row) => !row.dataset.pendingRemoval);
      const position = rows.indexOf(index.closest('[data-video-card]'));
      if (position >= 0) index.textContent = String(position + 1);
    });
  }

  /* Totals such as runtime are the server's to compute, so they are re-read rather than
     recalculated from whatever happens to be on the page. */
  async function refreshTotals() {
    if (!document.querySelector('main [data-video-card]')) {
      window.location.reload();
      return;
    }
    try {
      const html = await fetch(window.location.href).then((response) => response.text());
      const next = new DOMParser().parseFromString(html, 'text/html');
      let filtered = false;
      ['.page-head .page-meta', '.playlist-meta'].forEach((selector) => {
        const current = document.querySelector(selector);
        const fresh = next.querySelector(selector);
        if (!current || !fresh) return;
        /* While a filter is on, the header shows the filtered count; the total waits. */
        if (current.dataset.metaBase !== undefined) {
          current.dataset.metaBase = fresh.textContent;
          filtered = true;
        } else {
          current.textContent = fresh.textContent;
        }
      });
      if (filtered) refilter();
    } catch (error) { /* A stale total is not worth interrupting anyone for. */ }
  }

  /* ---- remove with undo ----
     The row leaves at once and the delete is held for the toast's lifetime. Filtering
     uses `hidden` too, so a held row gives up its filter text until it is settled. */
  const held = new Map();

  function removeWithUndo(row, { url, message, failure }) {
    if (!row || row.dataset.pendingRemoval) return;
    const filterText = row.dataset.filterItem;
    row.dataset.pendingRemoval = '1';
    delete row.dataset.filterItem;
    row.hidden = true;
    syncGroups();

    const restore = () => {
      delete row.dataset.pendingRemoval;
      if (filterText !== undefined) row.dataset.filterItem = filterText;
      row.hidden = false;
      syncGroups();
      refilter();
    };
    let timer = 0;
    let notice = null;
    const commit = async (leaving = false) => {
      window.clearTimeout(timer);
      held.delete(row);
      notice?.dismiss();
      try {
        const response = await fetch(url, { method: 'DELETE', keepalive: leaving });
        if (!response.ok && response.status !== 404) throw new Error('remove failed');
        if (leaving) return;
        row.remove();
        refreshTotals();
      } catch (error) {
        if (leaving) return;
        restore();
        toast(failure, { kind: 'error' });
      }
    };
    held.set(row, commit);
    timer = window.setTimeout(commit, UNDO_MS);
    notice = toast(message, {
      actionLabel: 'Undo',
      duration: UNDO_MS,
      action: () => {
        window.clearTimeout(timer);
        held.delete(row);
        restore();
      },
    });
  }
  /* Leaving the page inside the window still honours the removal the toast announced. */
  window.addEventListener('pagehide', () => held.forEach((commit) => commit(true)));

  /* ---- filtered empty state ----
     The page script applies the filter on the input itself; this listens further up the
     tree, so it always sees the result. */
  /* The page header's count follows the filter, "3 of 40 videos", and comes back when
     the filter is cleared. A header that is not a count says how many match instead. */
  function syncMeta(needle) {
    const meta = document.querySelector('.page-head .page-meta');
    if (!meta) return;
    if (!needle) {
      if (meta.dataset.metaBase !== undefined) {
        meta.textContent = meta.dataset.metaBase;
        delete meta.dataset.metaBase;
      }
      return;
    }
    if (meta.dataset.metaBase === undefined) meta.dataset.metaBase = meta.textContent;
    const base = meta.dataset.metaBase;
    const items = [...document.querySelectorAll('main [data-filter-item]')];
    const rows = items.length ? items : [...document.querySelectorAll('main [data-filter-group] [data-video-card]')];
    const shown = rows.filter((row) => !row.hidden && !row.closest('[hidden]')).length;
    meta.textContent = /^\d[\d,]*\+? \S+$/.test(base) ? `${shown} of ${base}` : `${shown} ${shown === 1 ? 'match' : 'matches'}`;
  }

  document.addEventListener('input', (event) => {
    if (!event.target.matches?.('[data-filter-input]')) return;
    syncGroups();
    syncMeta(event.target.value.trim());
    const empty = document.querySelector('[data-filter-empty]');
    if (!empty) return;
    const needle = event.target.value.trim();
    const anyLeft = [...document.querySelectorAll('main [data-filter-item]')].some((item) => !item.hidden);
    empty.hidden = !needle || anyLeft;
    const heading = empty.querySelector('h2');
    if (heading) heading.textContent = `No ${empty.dataset.filterEmpty} matches “${needle}”`;
  });

  /* ---- row actions ---- */
  document.addEventListener('click', async (event) => {
    const clear = event.target.closest('[data-filter-clear]');
    if (clear) {
      document.querySelectorAll('[data-filter-input]').forEach((input) => { input.value = ''; });
      refilter();
      document.querySelector('[data-filter-input]')?.focus();
      return;
    }

    const history = event.target.closest('[data-remove-history]');
    if (history) {
      event.preventDefault();
      history.closest('.yt-menu')?.removeAttribute('open');
      removeWithUndo(history.closest('[data-video-card]'), {
        url: `/api/history/${encodeURIComponent(history.dataset.removeHistory)}`,
        message: 'Removed from history',
        failure: 'Could not remove that video from history',
      });
      return;
    }

    const item = event.target.closest('[data-remove-playlist-item]');
    if (item) {
      event.preventDefault();
      item.closest('.yt-menu')?.removeAttribute('open');
      removeWithUndo(item.closest('[data-playlist-row]'), {
        url: `/api/playlists/${encodeURIComponent(item.dataset.playlistId)}/items/${encodeURIComponent(item.dataset.removePlaylistItem)}`,
        message: 'Removed from playlist',
        failure: 'Could not remove that video from the playlist',
      });
      return;
    }

    const download = event.target.closest('[data-remove-download]');
    if (download) {
      event.preventDefault();
      download.closest('.yt-menu')?.removeAttribute('open');
      const allowed = await window.ofConfirm({
        title: 'Delete the downloaded file?',
        body: 'The file leaves this server. The video stays in your feed.',
        confirmLabel: 'Delete',
        danger: true,
      });
      if (!allowed) return;
      download.disabled = true;
      try {
        const response = await fetch(`/api/downloads/${encodeURIComponent(download.dataset.removeDownload)}`, { method: 'DELETE' });
        if (!response.ok) throw new Error('delete failed');
        download.closest('[data-video-card]')?.remove();
        toast('Download deleted', { kind: 'success' });
        refreshTotals();
      } catch (error) {
        download.disabled = false;
        toast('Could not delete that download', { kind: 'error' });
      }
    }
  });

  /* ---- save to playlist ----
     One field finds and creates, and each row is a live toggle: the mark flips at once,
     the server is told, and a refusal puts it back and says so. */
  const picker = document.getElementById('playlist-picker-dialog');
  if (picker) {
    const list = picker.querySelector('[data-picker-list]');
    const filter = picker.querySelector('[data-picker-filter]');
    const subtitle = picker.querySelector('[data-picker-subtitle]');
    const CHECK = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>';
    const PLUS = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14"/><path d="M5 12h14"/></svg>';
    /* A modal makes the rest of the page inert, toasts included, so an error shown over
       it cannot wait to be dismissed the way errors usually do. */
    const PICKER_ERROR = { kind: 'error', duration: 5000 };
    let videoId = '';
    let items = [];
    let createRow = null;

    function paint(item) {
      item.row.setAttribute('aria-checked', item.has_video ? 'true' : 'false');
      item.row.querySelector('small').textContent = plural(item.video_count, 'video');
    }

    function buildRow(item) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'save-row';
      row.setAttribute('role', 'checkbox');
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
      copy.append(name, document.createElement('small'));
      const check = document.createElement('span');
      check.className = 'save-check';
      check.innerHTML = CHECK;
      row.append(cover, copy, check);
      row.addEventListener('click', () => toggle(item));
      item.row = row;
      paint(item);
      return row;
    }

    function applyFilter() {
      /* Typing before the list has loaded: render() applies the filter once it is in. */
      if (!createRow) return;
      const query = filter.value.trim();
      const needle = query.toLowerCase();
      items.forEach((item) => { item.row.hidden = Boolean(needle) && !item.name.toLowerCase().includes(needle); });
      const exact = items.some((item) => item.name.toLowerCase() === needle);
      createRow.hidden = !query || exact;
      createRow.querySelector('span:last-child').textContent = `Create “${query}”`;
      list.querySelector('.picker-empty')?.toggleAttribute('hidden', Boolean(query));
    }

    function render() {
      list.replaceChildren();
      createRow = document.createElement('button');
      createRow.type = 'button';
      createRow.className = 'save-row save-create';
      createRow.hidden = true;
      createRow.innerHTML = `<span class="save-cover">${PLUS}</span><span></span>`;
      createRow.addEventListener('click', create);
      list.append(createRow);
      if (!items.length) {
        const empty = document.createElement('p');
        empty.className = 'picker-empty';
        empty.textContent = 'No playlists yet. Type a name above to create one.';
        list.append(empty);
      }
      items.forEach((item) => list.append(buildRow(item)));
      applyFilter();
    }

    async function setMembership(playlistId, member) {
      const base = `/api/playlists/${encodeURIComponent(playlistId)}/items`;
      const response = member
        ? await fetch(base, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ youtube_video_id: videoId }) })
        : await fetch(`${base}/${encodeURIComponent(videoId)}`, { method: 'DELETE' });
      if (!response.ok) throw new Error('membership failed');
    }

    async function toggle(item) {
      if (item.busy) return;
      item.busy = true;
      const member = !item.has_video;
      item.has_video = member;
      item.video_count = Math.max(0, item.video_count + (member ? 1 : -1));
      paint(item);
      try {
        await setMembership(item.id, member);
        toast(`${member ? 'Saved to' : 'Removed from'} ${item.name}`, { kind: 'success' });
      } catch (error) {
        item.has_video = !member;
        item.video_count = Math.max(0, item.video_count + (member ? -1 : 1));
        paint(item);
        toast(`Could not update ${item.name}`, PICKER_ERROR);
      } finally {
        item.busy = false;
      }
    }

    async function create() {
      const name = filter.value.trim();
      if (!name || !createRow || createRow.hidden) return;
      const item = { id: '', name, video_count: 1, has_video: true, busy: true, cover_url: '' };
      items.unshift(item);
      list.querySelector('.picker-empty')?.remove();
      createRow.after(buildRow(item));
      filter.value = '';
      applyFilter();
      try {
        const response = await fetch('/api/playlists', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, description: null }),
        });
        if (!response.ok) throw new Error('create failed');
        item.id = (await response.json()).playlist?.id;
      } catch (error) {
        items = items.filter((entry) => entry !== item);
        item.row.remove();
        filter.value = name;
        applyFilter();
        toast(`Could not create ${name}`, PICKER_ERROR);
        return;
      }
      try {
        await setMembership(item.id, true);
        toast(`Saved to ${name}`, { kind: 'success' });
      } catch (error) {
        item.has_video = false;
        item.video_count = 0;
        paint(item);
        toast(`Created ${name}, but could not add the video`, PICKER_ERROR);
      } finally {
        item.busy = false;
      }
    }

    filter.addEventListener('input', applyFilter);
    filter.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      if (createRow && !createRow.hidden) { create(); return; }
      const visible = items.filter((item) => !item.row.hidden);
      if (filter.value.trim() && visible.length === 1) toggle(visible[0]);
    });

    window.ofOpenSavePicker = async (id, title) => {
      if (!id) return;
      videoId = id;
      items = [];
      createRow = null;
      filter.value = '';
      subtitle.textContent = title || '';
      subtitle.hidden = !title;
      list.innerHTML = '<span class="sr-only">Loading playlists</span>' + '<span class="skeleton save-row-skeleton"></span>'.repeat(3);
      picker.showModal();
      if (window.matchMedia('(pointer: fine)').matches) filter.focus();
      try {
        const response = await fetch(`/api/playlists?video=${encodeURIComponent(id)}`);
        if (!response.ok) throw new Error('load failed');
        items = ((await response.json()).items || []).map((item) => ({
          ...item,
          name: item.name || 'Untitled playlist',
          video_count: Number(item.video_count || 0),
        }));
        if (videoId === id) render();
      } catch (error) {
        list.innerHTML = '<p class="picker-empty">Could not load your playlists.</p>';
      }
    };
  }

  /* ---- downloads page ---- */
  const page = document.querySelector('[data-downloads-page]');
  if (page) {
    const ACTIVE_POLL_MS = 2000;
    const IDLE_POLL_MS = 10000;
    const DISMISSED_KEY = 'openfeed.dismissedDownloadJobs';
    const activeSection = page.querySelector('[data-downloads-active]');
    const activeList = page.querySelector('[data-downloads-active-list]');
    const failedSection = page.querySelector('[data-downloads-failed]');
    const failedList = page.querySelector('[data-downloads-failed-list]');
    const jobTemplate = page.querySelector('[data-download-job-template]');
    const failedTemplate = page.querySelector('[data-download-failed-template]');
    const running = new Set();
    let timer = 0;

    const WORDING = { queued: 'Queued', downloading: 'Downloading', merging: 'Merging video and audio' };

    function formatSize(bytes) {
      const units = ['B', 'KB', 'MB', 'GB', 'TB'];
      let value = Number(bytes) || 0;
      let unit = 0;
      while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
      return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
    }

    function formatEta(seconds) {
      if (!seconds || seconds < 0) return '';
      const minutes = Math.floor(seconds / 60);
      return minutes ? `${minutes}m ${Math.round(seconds % 60)}s left` : `${Math.round(seconds)}s left`;
    }

    /* The server drops failed jobs on its own retention timer and offers no delete, so
       Remove is this browser's choice to stop showing one. */
    function dismissed() {
      try {
        return new Set(JSON.parse(window.localStorage.getItem(DISMISSED_KEY) || '[]'));
      } catch (error) {
        return new Set();
      }
    }

    function dismiss(id) {
      try {
        window.localStorage.setItem(DISMISSED_KEY, JSON.stringify([...dismissed(), id].slice(-50)));
      } catch (error) { /* The row still leaves for now. */ }
    }

    function fillJob(row, job) {
      const quality = job.quality || '';
      row.querySelector('[data-job-title]').textContent = job.title || job.youtube_video_id;
      const progress = job.total_bytes ? `${formatSize(job.downloaded_bytes)} of ${formatSize(job.total_bytes)}` : '';
      const speed = job.speed_bps ? `${formatSize(job.speed_bps)}/s` : '';
      row.querySelector('[data-job-stats]').textContent = [WORDING[job.status] || job.status, quality, progress, speed, formatEta(job.eta_seconds)]
        .filter(Boolean).join(' · ');
      /* The total is unknown until yt-dlp reports it, and the merge has no percentage. */
      const bar = row.querySelector('[data-job-bar]');
      const indeterminate = job.status === 'merging' || !job.total_bytes;
      bar.classList.toggle('is-indeterminate', indeterminate);
      bar.querySelector('span').style.width = indeterminate ? '' : `${job.percent || 0}%`;
    }

    function jobRow(job, list, template) {
      let row = list.querySelector(`[data-job-id="${CSS.escape(job.id)}"]`);
      if (!row) {
        row = template.content.firstElementChild.cloneNode(true);
        row.dataset.jobId = job.id;
        row.querySelector('[data-job-thumb]')?.setAttribute('src', `/vi/${encodeURIComponent(job.youtube_video_id)}/hqdefault.jpg`);
        list.append(row);
      }
      return row;
    }

    function sync(list, section, ids) {
      list.querySelectorAll(':scope > :not([data-job-id])').forEach((row) => row.remove());
      list.querySelectorAll('[data-job-id]').forEach((row) => { if (!ids.has(row.dataset.jobId)) row.remove(); });
      section.hidden = !ids.size;
    }

    function render(jobs) {
      const hide = dismissed();
      const active = jobs.filter((job) => job.status in WORDING);
      const failed = jobs.filter((job) => job.status === 'error' && !hide.has(job.id));
      active.forEach((job) => fillJob(jobRow(job, activeList, jobTemplate), job));
      failed.forEach((job) => {
        const row = jobRow(job, failedList, failedTemplate);
        row.job = job;
        row.querySelector('[data-job-title]').textContent = job.title || job.youtube_video_id;
        const cause = String(job.error || '').replace(/^ERROR:\s*/, '') || 'the download stopped without giving a reason';
        row.querySelector('[data-job-error]').textContent = `Failed: ${cause}`;
      });
      sync(activeList, activeSection, new Set(active.map((job) => job.id)));
      sync(failedList, failedSection, new Set(failed.map((job) => job.id)));
      const finished = jobs.some((job) => running.has(job.id) && job.status === 'done' && job.destination === 'server');
      running.clear();
      active.forEach((job) => running.add(job.id));
      /* The library rows are server-rendered, so a finished job means a fresh page. */
      if (finished) window.location.reload();
      return active.length;
    }

    async function poll() {
      window.clearTimeout(timer);
      let delay = IDLE_POLL_MS;
      if (!document.hidden) {
        try {
          const response = await fetch('/api/download/jobs');
          if (!response.ok) throw new Error('jobs failed');
          if (render((await response.json()).items || [])) delay = ACTIVE_POLL_MS;
        } catch (error) { /* The next tick tries again; a row is never invented meanwhile. */ }
      }
      timer = window.setTimeout(poll, delay);
    }

    page.addEventListener('click', async (event) => {
      const cancel = event.target.closest('[data-job-cancel]');
      if (cancel) {
        cancel.disabled = true;
        const id = cancel.closest('[data-job-id]').dataset.jobId;
        try {
          const response = await fetch(`/api/download/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' });
          if (!response.ok) throw new Error('cancel failed');
          toast('Download cancelled');
        } catch (error) {
          cancel.disabled = false;
          toast('Could not cancel that download', { kind: 'error' });
        }
        poll();
        return;
      }

      const remove = event.target.closest('[data-job-dismiss]');
      if (remove) {
        const row = remove.closest('[data-job-id]');
        dismiss(row.dataset.jobId);
        row.remove();
        failedSection.hidden = !failedList.children.length;
        return;
      }

      const retry = event.target.closest('[data-job-retry]');
      if (retry) {
        const row = retry.closest('[data-job-id]');
        const { job } = row;
        retry.disabled = true;
        try {
          const response = await fetch(`/api/download/${encodeURIComponent(job.youtube_video_id)}/jobs`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ height: job.height, container: job.container, needs_merge: false, title: job.title, destination: job.destination }),
          });
          const data = await response.json();
          if (!response.ok) throw new Error(data.detail || 'The server refused the retry');
          dismiss(job.id);
          row.remove();
          failedSection.hidden = !failedList.children.length;
          toast('Retrying the download');
        } catch (error) {
          retry.disabled = false;
          toast(`Could not retry: ${error.message}`, { kind: 'error' });
        }
        poll();
      }
    });

    document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
    poll();
  }
})();
