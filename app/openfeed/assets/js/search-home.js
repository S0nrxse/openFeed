/* Search type-ahead and recent searches, one-tap Watch later, Undo for feedback, the Not
   interested list, pull to refresh and Back to top. Bundled ahead of shell.js and ui-kit.js,
   so window.ofToast is only called from event handlers, never while this file loads.
   Recent searches share shell.js's storage key, so the phone takeover and the desktop
   box list the same searches. */
(() => {
  'use strict';

  const SVG = (paths) => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${paths}</svg>`;
  const ICONS = {
    history: SVG('<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>'),
    search: SVG('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>'),
    close: SVG('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'),
    refresh: SVG('<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>'),
    up: SVG('<path d="m18 15-6-6-6 6"/>'),
  };
  const toast = (message, options) => window.ofToast?.(message, options);
  const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const postJson = (url, body, extra = {}) => fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), ...extra,
  });

  /* ---------------- recent searches ---------------- */
  const RECENT_KEY = 'openfeed-recent-searches';
  const RECENT_MAX = 8;
  function readRecent() {
    try {
      const stored = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
      return Array.isArray(stored) ? stored.filter((item) => typeof item === 'string').slice(0, RECENT_MAX) : [];
    } catch (error) {
      return [];
    }
  }
  function writeRecent(items) {
    try {
      localStorage.setItem(RECENT_KEY, JSON.stringify(items.slice(0, RECENT_MAX)));
    } catch (error) { /* a private window simply has no recents */ }
  }
  function addRecent(term) {
    const text = String(term || '').trim();
    if (text) writeRecent([text, ...readRecent().filter((item) => item !== text)]);
  }
  function removeRecent(term) {
    writeRecent(readRecent().filter((item) => item !== term));
  }

  /* Every search form records what it sends. The phone takeover records on its own too;
     the list is deduplicated, so a second write only moves the term to the front. */
  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!form.matches?.('form[role="search"]')) return;
    const input = form.querySelector('input[name="q"]');
    if (input) addRecent(input.value);
    const state = boxes.get(input);
    if (state) close(state);
  });

  /* ---------------- type-ahead ---------------- */
  const SUGGEST_DELAY = 120;
  const SUGGEST_MAX = 10;
  const MATCHING_RECENT_MAX = 3;
  const remote = new Map();
  const boxes = new Map();
  let boxCount = 0;

  function fetchSuggestions(query, signal) {
    const key = query.toLowerCase();
    if (remote.has(key)) return Promise.resolve(remote.get(key));
    return fetch(`/api/search/suggestions?q=${encodeURIComponent(query)}`, { signal })
      .then((response) => (response.ok ? response.json() : { suggestions: [] }))
      .then((data) => {
        const items = Array.isArray(data.suggestions) ? data.suggestions.filter((item) => typeof item === 'string') : [];
        remote.set(key, items);
        if (remote.size > 200) remote.delete(remote.keys().next().value);
        return items;
      });
  }

  /* What the list shows: your own matching searches first, then YouTube's. */
  function merge(typed, suggestions) {
    const needle = typed.trim().toLowerCase();
    const recent = readRecent();
    const mine = needle
      ? [
        ...recent.filter((term) => term.toLowerCase().startsWith(needle)),
        ...recent.filter((term) => !term.toLowerCase().startsWith(needle) && term.toLowerCase().includes(needle)),
      ].slice(0, MATCHING_RECENT_MAX)
      : recent;
    const seen = new Set(mine.map((term) => term.toLowerCase()));
    const theirs = (needle ? suggestions : []).filter((term) => {
      const key = term.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    return [
      ...mine.map((text) => ({ text, recent: true })),
      ...theirs.map((text) => ({ text, recent: false })),
    ].slice(0, needle ? SUGGEST_MAX : RECENT_MAX);
  }

  /* The typed part plain and the completion bold, the way YouTube draws it. */
  function labelFor(text, typed) {
    const span = document.createElement('span');
    span.className = 'suggest-text';
    const head = typed.trim();
    if (head && text.toLowerCase().startsWith(head.toLowerCase()) && text.length > head.length) {
      span.append(text.slice(0, head.length));
      const rest = document.createElement('b');
      rest.textContent = text.slice(head.length);
      span.append(rest);
    } else {
      span.textContent = text;
    }
    return span;
  }

  function ensureBox(input) {
    if (boxes.has(input)) return boxes.get(input);
    const form = input.closest('form');
    const sheet = input.closest('.search-sheet');
    const id = `search-suggest-${++boxCount}`;
    const panel = document.createElement('div');
    panel.className = sheet ? 'suggest-panel is-inline' : 'suggest-panel';
    panel.id = id;
    panel.setAttribute('role', 'listbox');
    panel.setAttribute('aria-label', 'Search suggestions');
    panel.hidden = true;
    if (sheet) sheet.querySelector('.search-sheet-body')?.before(panel);
    else form.after(panel);
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-controls', id);
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('autocomplete', 'off');
    const state = { input, form, sheet, panel, items: [], active: -1, typed: input.value, timer: 0, abort: null, open: false };
    boxes.set(input, state);

    /* Keep focus in the field while a row is pressed, so the list is still there for the click. */
    panel.addEventListener('mousedown', (event) => event.preventDefault());
    panel.addEventListener('click', (event) => {
      const remove = event.target.closest('[data-suggest-remove]');
      const row = event.target.closest('[data-suggest-index]');
      if (remove && row) {
        event.preventDefault();
        removeRecent(state.items[Number(row.dataset.suggestIndex)]?.text);
        show(state, state.typed);
        input.focus();
        return;
      }
      if (row) choose(state, Number(row.dataset.suggestIndex));
    });
    return state;
  }

  function place(state) {
    if (state.sheet) return;
    const { form, panel } = state;
    /* Measured against whatever the panel is positioned in: the fixed header for the top
       box, the page for the phone results page's own box. */
    const host = panel.offsetParent || document.documentElement;
    const hostRect = host.getBoundingClientRect();
    const rect = form.getBoundingClientRect();
    panel.style.left = `${rect.left - hostRect.left - host.clientLeft + host.scrollLeft}px`;
    panel.style.top = `${rect.bottom - hostRect.top - host.clientTop + host.scrollTop + 4}px`;
    panel.style.width = `${rect.width}px`;
  }

  function render(state) {
    const { panel, items, input } = state;
    panel.replaceChildren();
    items.forEach((item, index) => {
      const row = document.createElement('div');
      row.className = item.recent ? 'suggest-row is-recent' : 'suggest-row';
      row.id = `${panel.id}-${index}`;
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', index === state.active ? 'true' : 'false');
      row.dataset.suggestIndex = String(index);
      row.insertAdjacentHTML('beforeend', item.recent ? ICONS.history : ICONS.search);
      row.append(labelFor(item.text, state.typed));
      if (item.recent) {
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'suggest-remove';
        remove.tabIndex = -1;
        remove.dataset.suggestRemove = '';
        remove.setAttribute('aria-label', `Remove ${item.text} from recent searches`);
        remove.title = 'Remove';
        remove.innerHTML = ICONS.close;
        row.append(remove);
      }
      panel.append(row);
    });
    const open = items.length > 0;
    state.open = open;
    panel.hidden = !open;
    input.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (state.active >= 0 && open) input.setAttribute('aria-activedescendant', `${panel.id}-${state.active}`);
    else input.removeAttribute('aria-activedescendant');
    if (state.sheet) {
      /* Typing in the phone takeover swaps its recent list for the suggestions. */
      const body = state.sheet.querySelector('.search-sheet-body');
      if (body) body.hidden = open && Boolean(state.typed.trim());
    }
    if (open) place(state);
  }

  function show(state, typed) {
    state.typed = typed;
    state.active = -1;
    state.items = merge(typed, remote.get(typed.trim().toLowerCase()) || []);
    if (state.sheet && !typed.trim()) state.items = [];
    render(state);
  }

  function request(state) {
    window.clearTimeout(state.timer);
    state.abort?.abort();
    const typed = state.input.value;
    show(state, typed);
    const query = typed.trim();
    if (!query) return;
    state.timer = window.setTimeout(() => {
      const controller = new AbortController();
      state.abort = controller;
      fetchSuggestions(query, controller.signal)
        .then(() => {
          if (state.input.value !== typed || document.activeElement !== state.input) return;
          show(state, typed);
        })
        .catch(() => { /* recents alone are still useful */ });
    }, SUGGEST_DELAY);
  }

  function close(state) {
    window.clearTimeout(state.timer);
    state.abort?.abort();
    state.items = [];
    state.active = -1;
    render(state);
  }

  function choose(state, index) {
    const item = state.items[index];
    if (!item) return;
    state.input.value = item.text;
    close(state);
    if (typeof state.form.requestSubmit === 'function') {
      state.form.requestSubmit();
    } else {
      window.ofNavPending?.();
      state.form.submit();
    }
  }

  function move(state, step) {
    if (!state.open) {
      request(state);
      return;
    }
    const count = state.items.length;
    let next = state.active + step;
    if (next >= count) next = -1;
    if (next < -1) next = count - 1;
    state.active = next;
    state.input.value = next >= 0 ? state.items[next].text : state.typed;
    const typed = state.typed;
    render(state);
    state.typed = typed;
    state.panel.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
  }

  const fieldFor = (target) => target?.closest?.('form[role="search"] input[name="q"]');

  document.addEventListener('focusin', (event) => {
    const input = fieldFor(event.target);
    if (!input) return;
    const state = ensureBox(input);
    if (!state.open) request(state);
  });
  document.addEventListener('input', (event) => {
    const input = fieldFor(event.target);
    if (!input) return;
    request(ensureBox(input));
  });
  document.addEventListener('focusout', (event) => {
    const state = boxes.get(fieldFor(event.target));
    if (!state) return;
    window.setTimeout(() => {
      if (document.activeElement !== state.input) close(state);
    }, 0);
  });
  document.addEventListener('keydown', (event) => {
    const state = boxes.get(fieldFor(event.target));
    if (!state) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      move(state, event.key === 'ArrowDown' ? 1 : -1);
    } else if (event.key === 'Escape' && state.open) {
      /* Closes the list first; a second Escape reaches the takeover or the page. */
      event.preventDefault();
      event.stopPropagation();
      state.input.value = state.active >= 0 ? state.input.value : state.typed;
      close(state);
    } else if (event.key === 'Enter' && state.open && state.active >= 0) {
      event.preventDefault();
      choose(state, state.active);
    } else if (event.key === 'Delete' && event.shiftKey && state.open && state.items[state.active]?.recent) {
      event.preventDefault();
      removeRecent(state.items[state.active].text);
      state.input.value = state.typed;
      show(state, state.typed);
    } else if (event.key === 'Tab' && state.open) {
      close(state);
    }
  }, true);
  window.addEventListener('resize', () => boxes.forEach((state) => state.open && place(state)));

  /* ---------------- Watch later ---------------- */
  let watchLater = null;
  let watchLaterLoad = null;

  function loadWatchLater() {
    if (!watchLaterLoad) {
      watchLaterLoad = fetch('/api/watch-later')
        .then((response) => (response.ok ? response.json() : { ids: [] }))
        .then((data) => { watchLater = new Set(Array.isArray(data.ids) ? data.ids : []); })
        .catch(() => { watchLater = new Set(); watchLaterLoad = null; });
    }
    return watchLaterLoad;
  }

  function paintLater(control) {
    if (!watchLater) return;
    const saved = watchLater.has(control.dataset.watchLater);
    if (control.classList.contains('card-quick-button')) {
      if (control.getAttribute('aria-pressed') === String(saved)) return;
      control.setAttribute('aria-pressed', saved ? 'true' : 'false');
      control.setAttribute('aria-label', saved ? 'Remove from Watch later' : 'Save to Watch later');
      control.title = saved ? 'Saved to Watch later' : 'Watch later';
      /* The clock stays; saved inverts it (Bone), since a check here would read as Mark watched. */
    } else {
      const label = control.querySelector('.menu-label');
      if (label) label.textContent = saved ? 'Remove from Watch later' : 'Save to Watch later';
    }
  }
  function paintLaterIn(root) {
    root.querySelectorAll?.('[data-watch-later]').forEach(paintLater);
  }
  function paintLaterFor(videoId) {
    const match = window.CSS?.escape ? CSS.escape(videoId) : videoId;
    document.querySelectorAll(`[data-watch-later="${match}"]`).forEach(paintLater);
  }

  /* State is read when a card is pointed at, focused or its menu opens, so a page full of
     cards costs one request, made the first time it matters. */
  function syncCard(target) {
    const card = target?.closest?.('[data-video-card]');
    if (!card || !card.querySelector('[data-watch-later]')) return;
    if (watchLater) paintLaterIn(card);
    else loadWatchLater().then(() => paintLaterIn(card));
  }
  document.addEventListener('pointerover', (event) => syncCard(event.target), { passive: true });
  document.addEventListener('focusin', (event) => syncCard(event.target));
  document.addEventListener('toggle', (event) => {
    if (event.target.matches?.('details.yt-menu') && event.target.open) syncCard(event.target);
  }, true);

  async function setWatchLater(videoId, saved, { undo = true } = {}) {
    const response = await postJson('/api/watch-later', { youtube_video_id: videoId, saved });
    if (!response.ok) throw new Error('watch later failed');
    if (saved) watchLater.add(videoId);
    else watchLater.delete(videoId);
    paintLaterFor(videoId);
    const message = saved ? 'Saved to Watch later' : 'Removed from Watch later';
    if (!undo) {
      toast(message);
      return;
    }
    toast(message, {
      actionLabel: 'Undo',
      action: () => setWatchLater(videoId, !saved, { undo: false })
        .catch(() => toast('Could not undo', { kind: 'error' })),
    });
  }

  document.addEventListener('click', async (event) => {
    const control = event.target.closest?.('[data-watch-later]');
    if (!control) return;
    event.preventDefault();
    const videoId = control.dataset.watchLater;
    control.closest('.yt-menu')?.removeAttribute('open');
    if (!videoId || control.dataset.busy) return;
    control.dataset.busy = '1';
    try {
      await loadWatchLater();
      if (!watchLater) throw new Error('state unavailable');
      await setWatchLater(videoId, !watchLater.has(videoId));
    } catch (error) {
      toast('Could not update Watch later', { kind: 'error' });
    } finally {
      delete control.dataset.busy;
    }
  });

  /* ---------------- More like this / Less like this: Undo ----------------
     The page script records the press and hands the response here. Undo removes that
     event and puts the video's feedback back as the server said it was before. */
  window.ofFeedbackToast = (videoId, eventType, data) => {
    const message = eventType === 'more_like_this' ? 'Noted. You’ll see more like this' : 'Noted. You’ll see less like this';
    const eventId = data?.event?.id;
    if (!eventId || !data?.previous) {
      toast(message);
      return;
    }
    toast(message, {
      actionLabel: 'Undo',
      action: async () => {
        try {
          const response = await postJson('/api/feedback/undo', { youtube_video_id: videoId, event_id: eventId, previous: data.previous });
          if (!response.ok) throw new Error('undo failed');
          toast('Feedback undone');
        } catch (error) {
          toast('Could not undo', { kind: 'error' });
        }
      },
    });
  };

  /* ---------------- the Not interested list ----------------
     Same held removal as the library rows: the row leaves at once and the change waits
     for the toast, so Undo never has to send anything. */
  const UNDO_MS = 6000;
  const held = new Map();

  function settleList() {
    document.querySelectorAll('.interest-section').forEach((section) => {
      section.hidden = !section.querySelector('[data-video-card]:not([hidden]), [data-interest-row]:not([hidden])');
    });
    const meta = document.querySelector('.page-head .page-meta');
    if (meta && meta.dataset.metaBase === undefined) {
      const left = document.querySelectorAll('.interest-section :is([data-video-card], [data-interest-row]):not([data-pending-removal])').length;
      meta.textContent = left ? `${left} item${left === 1 ? '' : 's'}` : '';
    }
  }

  function holdRemoval(row, send, message) {
    if (!row || row.dataset.pendingRemoval) return;
    const filterText = row.dataset.filterItem;
    row.dataset.pendingRemoval = '1';
    delete row.dataset.filterItem;
    row.hidden = true;
    settleList();
    let timer = 0;
    let notice = null;
    const restore = () => {
      delete row.dataset.pendingRemoval;
      if (filterText !== undefined) row.dataset.filterItem = filterText;
      row.hidden = false;
      settleList();
    };
    const commit = async (leaving = false) => {
      window.clearTimeout(timer);
      held.delete(row);
      notice?.dismiss();
      try {
        const response = await send(leaving);
        if (!response.ok && response.status !== 404) throw new Error('remove failed');
        if (leaving) return;
        row.remove();
        if (!document.querySelector('.interest-section :is([data-video-card], [data-interest-row])')) window.location.reload();
      } catch (error) {
        if (leaving) return;
        restore();
        toast('Could not remove that from the list', { kind: 'error' });
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
  window.addEventListener('pagehide', () => held.forEach((commit) => commit(true)));

  document.addEventListener('click', (event) => {
    const video = event.target.closest?.('[data-restore-video]');
    if (video) {
      event.preventDefault();
      video.closest('.yt-menu')?.removeAttribute('open');
      const id = video.dataset.restoreVideo;
      holdRemoval(video.closest('[data-video-card]'), (leaving) => fetch(`/api/not-interested/${encodeURIComponent(id)}`, { method: 'DELETE', keepalive: leaving }),
        'Removed from Not interested. It can be recommended again');
      return;
    }
    const channel = event.target.closest?.('[data-unmute-interest]');
    if (channel) {
      event.preventDefault();
      const key = channel.dataset.unmuteInterest;
      holdRemoval(channel.closest('[data-interest-row]'), (leaving) => postJson('/api/feed/muted-channels', { channel_key: key, muted: false }, { keepalive: leaving }),
        `${channel.dataset.channelTitle || 'Channel'} can be recommended again`);
    }
  });

  /* ---------------- pull to refresh (Home and Subscriptions, touch only) ---------------- */
  const PULL_THRESHOLD = 64;
  const PULL_MAX = 96;

  function setupPull() {
    const tab = document.querySelector('.mobile-nav-item[aria-current="page"]');
    const where = tab?.getAttribute('href');
    const home = where === '/' && document.querySelector('[data-feed-container]');
    const subscriptions = where === '/subscriptions' && document.querySelector('[data-view-target]');
    if (!home && !subscriptions) return;
    if (!('ontouchstart' in window) && !window.matchMedia('(pointer: coarse)').matches) return;
    /* The browser's own pull would reload the whole page on top of this one. */
    document.documentElement.classList.add('has-pull-refresh');

    const indicator = document.createElement('div');
    indicator.className = 'pull-refresh';
    indicator.setAttribute('aria-hidden', 'true');
    indicator.innerHTML = `<span class="pull-refresh-disc">${ICONS.refresh}</span>`;
    const status = document.createElement('p');
    status.className = 'sr-only';
    status.setAttribute('role', 'status');
    document.body.append(indicator, status);

    let startX = 0;
    let startY = 0;
    let distance = 0;
    let pulling = false;
    let tracking = false;
    let busy = false;

    const paint = () => {
      indicator.style.setProperty('--pull', `${distance}px`);
      indicator.style.setProperty('--pull-turn', `${Math.round(distance * 3.2)}deg`);
      indicator.classList.toggle('is-armed', distance >= PULL_THRESHOLD);
      indicator.classList.toggle('is-pulling', distance > 0);
    };
    const reset = () => {
      distance = 0;
      pulling = false;
      indicator.classList.remove('is-refreshing');
      paint();
    };
    const blocked = (target) => busy || window.scrollY > 0
      || document.querySelector('dialog[open], .yt-menu[open]')
      || target.closest?.('[data-shelf-track], .topic-row, input, textarea, select');

    async function refresh() {
      busy = true;
      indicator.classList.add('is-refreshing');
      distance = PULL_THRESHOLD;
      paint();
      status.textContent = 'Refreshing';
      try {
        if (home) {
          await window.ofReloadFeed?.();
        } else {
          const response = await fetch(window.location.href, { headers: { Accept: 'text/html' } });
          if (!response.ok) throw new Error('refresh failed');
          const next = new DOMParser().parseFromString(await response.text(), 'text/html').querySelector('[data-view-target]');
          if (!next) throw new Error('refresh failed');
          subscriptions.innerHTML = next.innerHTML;
        }
        status.textContent = 'Refreshed';
      } catch (error) {
        status.textContent = '';
        toast('Could not refresh', { kind: 'error' });
      } finally {
        busy = false;
        reset();
      }
    }

    document.addEventListener('touchstart', (event) => {
      tracking = event.touches.length === 1 && !blocked(event.target);
      if (!tracking) return;
      startX = event.touches[0].clientX;
      startY = event.touches[0].clientY;
      pulling = false;
    }, { passive: true });
    document.addEventListener('touchmove', (event) => {
      if (!tracking) return;
      const dx = event.touches[0].clientX - startX;
      const dy = event.touches[0].clientY - startY;
      if (!pulling) {
        if (dy < 10 || Math.abs(dx) > dy || window.scrollY > 0) {
          if (Math.abs(dx) > 10 || dy < -10) tracking = false;
          return;
        }
        pulling = true;
      }
      distance = Math.max(0, Math.min(PULL_MAX, (dy - 10) * 0.5));
      paint();
    }, { passive: true });
    const finish = () => {
      if (!tracking) return;
      tracking = false;
      if (pulling && distance >= PULL_THRESHOLD) refresh();
      else reset();
    };
    document.addEventListener('touchend', finish, { passive: true });
    document.addEventListener('touchcancel', () => { tracking = false; reset(); }, { passive: true });
  }

  /* ---------------- Back to top (desktop) ----------------
     On a phone, tapping the tab you are on already does this (shell.js). */
  function setupBackToTop() {
    if (document.querySelector('[data-watch-layout], [data-shorts-slide]')) return;
    const wide = window.matchMedia('(min-width: 901px)');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn back-to-top';
    button.hidden = true;
    button.innerHTML = `${ICONS.up}<span>Back to top</span>`;
    document.body.append(button);
    let queued = false;
    const sync = () => {
      queued = false;
      button.hidden = !(wide.matches && window.scrollY > window.innerHeight * 2.5);
    };
    window.addEventListener('scroll', () => {
      if (queued) return;
      queued = true;
      window.requestAnimationFrame(sync);
    }, { passive: true });
    wide.addEventListener?.('change', sync);
    button.addEventListener('click', () => {
      window.scrollTo({ top: 0, behavior: reducedMotion() ? 'auto' : 'smooth' });
      document.getElementById('main')?.focus({ preventScroll: true });
    });
  }

  const start = () => {
    setupPull();
    setupBackToTop();
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
