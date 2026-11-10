/* Shell behaviour that belongs to no single page: the search field, the phone search
   takeover, the You tab, the filter chip row and the watched state on cards. Bundled
   ahead of ui-kit.js, so nothing here may call window.ofToast while the file loads. */
(() => {
  'use strict';

  const ICON = (paths) => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${paths}</svg>`;
  const ICONS = {
    back: ICON('<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>'),
    close: ICON('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'),
    history: ICON('<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>'),
    fill: ICON('<path d="M17 17 7 7"/><path d="M7 16V7h9"/>'),
    check: ICON('<path d="M20 6 9 17l-5-5"/>'),
  };
  const smooth = () => (window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth');

  /* ---- search field ---- */
  document.addEventListener('click', (event) => {
    const clear = event.target.closest?.('[data-search-clear]');
    if (!clear) return;
    const input = clear.parentElement.querySelector('input');
    input.value = '';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.focus();
  });

  /* ---- search takeover ----
     Recent searches stay in this browser. The dialog is built on first use and sits in
     the top layer, so .app-header's backdrop-filter cannot trap it.

     A history entry gives the back gesture something to close. Submitting replaces that
     entry instead of adding to it, so Back from the results lands on the page the search
     was opened from rather than on a dead duplicate of it. */
  const RECENT_KEY = 'openfeed-recent-searches';
  const RECENT_MAX = 8;
  let sheet = null;
  let sheetInput = null;
  let recentBox = null;

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

  function goToResults(query) {
    const term = query.trim();
    if (!term) return;
    writeRecent([term, ...readRecent().filter((item) => item !== term)]);
    window.location.replace(`/search?q=${encodeURIComponent(term)}`);
  }

  function iconButton(icon, label, onClick) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'recent-action';
    button.setAttribute('aria-label', label);
    button.innerHTML = icon;
    button.addEventListener('click', onClick);
    return button;
  }

  function renderRecent() {
    const items = readRecent();
    recentBox.replaceChildren();
    if (!items.length) {
      const note = document.createElement('p');
      note.className = 'recent-empty';
      note.textContent = 'Your recent searches appear here. They stay on this device.';
      recentBox.append(note);
      return;
    }
    const head = document.createElement('div');
    head.className = 'recent-head';
    const label = document.createElement('h2');
    label.textContent = 'Recent';
    const clearAll = document.createElement('button');
    clearAll.type = 'button';
    clearAll.className = 'recent-clear';
    clearAll.textContent = 'Clear all';
    clearAll.addEventListener('click', () => {
      writeRecent([]);
      renderRecent();
      sheetInput.focus();
    });
    head.append(label, clearAll);
    const list = document.createElement('ul');
    list.className = 'recent-list';
    items.forEach((term) => {
      const row = document.createElement('li');
      row.className = 'recent-row';
      const link = document.createElement('a');
      link.href = `/search?q=${encodeURIComponent(term)}`;
      link.innerHTML = ICONS.history;
      const text = document.createElement('span');
      text.textContent = term;
      link.append(text);
      link.addEventListener('click', (event) => {
        event.preventDefault();
        goToResults(term);
      });
      row.append(
        link,
        iconButton(ICONS.fill, `Edit ${term}`, () => {
          sheetInput.value = term;
          sheetInput.focus();
        }),
        iconButton(ICONS.close, `Remove ${term}`, () => {
          writeRecent(readRecent().filter((item) => item !== term));
          renderRecent();
        }),
      );
      list.append(row);
    });
    recentBox.append(head, list);
  }

  function buildSheet() {
    sheet = document.createElement('dialog');
    sheet.className = 'search-sheet';
    sheet.setAttribute('aria-label', 'Search');
    sheet.innerHTML = `
      <form class="search-sheet-bar" action="/search" method="get" role="search">
        <button class="icon-button" type="button" data-search-close aria-label="Back">${ICONS.back}</button>
        <div class="search-sheet-field">
          <input name="q" type="search" placeholder="Search" aria-label="Search" enterkeyhint="search" autocomplete="off">
          <button class="search-clear" type="button" data-search-clear aria-label="Clear search">${ICONS.close}</button>
        </div>
      </form>
      <div class="search-sheet-body" data-search-recent></div>`;
    document.body.append(sheet);
    sheetInput = sheet.querySelector('input');
    recentBox = sheet.querySelector('[data-search-recent]');
    sheet.querySelector('form').addEventListener('submit', (event) => {
      event.preventDefault();
      goToResults(sheetInput.value);
    });
    sheet.querySelector('[data-search-close]').addEventListener('click', () => sheet.close());
    sheet.addEventListener('close', () => {
      if (history.state?.ofSearch) history.back();
    });
  }

  function openSearch() {
    if (!sheet) buildSheet();
    if (sheet.open) return;
    sheetInput.value = new URLSearchParams(window.location.search).get('q') || '';
    renderRecent();
    sheet.showModal();
    sheetInput.focus();
    history.pushState({ ofSearch: true }, '');
  }

  window.addEventListener('popstate', () => {
    if (sheet?.open && !history.state?.ofSearch) sheet.close();
  });
  window.addEventListener('pageshow', (event) => {
    if (event.persisted && sheet?.open) sheet.close();
  });

  /* ---- tab bar ---- */
  document.addEventListener('click', (event) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey) return;
    const tab = event.target.closest?.('.mobile-nav-item');
    if (!tab) return;
    if (tab.matches('[data-search-open]')) {
      event.preventDefault();
      openSearch();
    } else if (tab.matches('a[aria-current="page"]')) {
      /* Tapping the tab you are on goes back to its top. */
      event.preventDefault();
      window.scrollTo({ top: 0, behavior: smooth() });
    }
  });

  /* The page script closes menus on any click outside one, and this button is outside
     the menu it opens, so the click stops here. */
  document.querySelector('[data-open-account]')?.addEventListener('click', (event) => {
    event.stopPropagation();
    const menu = document.querySelector('.profile-menu');
    if (menu) menu.open = !menu.open;
  });

  /* ---- filter chips ---- */
  const chipWrap = document.querySelector('[data-topic-wrap]');
  const chipRow = chipWrap?.querySelector('[data-topic-row]');
  if (chipRow) {
    const syncChips = () => {
      chipWrap.classList.toggle('at-end', chipRow.scrollLeft >= chipRow.scrollWidth - chipRow.clientWidth - 4);
    };
    chipRow.addEventListener('scroll', syncChips, { passive: true });
    window.addEventListener('resize', syncChips);
    chipWrap.querySelector('[data-topic-next]')?.addEventListener('click', () => {
      chipRow.scrollBy({ left: chipRow.clientWidth * 0.7, behavior: smooth() });
    });
    /* A linked filter can be far along the row. */
    const active = chipRow.querySelector('.topic-chip.active');
    if (active && active !== chipRow.firstElementChild) {
      chipRow.scrollLeft += active.getBoundingClientRect().left - chipRow.getBoundingClientRect().left - 24;
    }
    syncChips();

    /* The row's hairline only appears once it is floating over the feed. */
    const sentinel = document.querySelector('[data-topic-sentinel]');
    if (sentinel && 'IntersectionObserver' in window) {
      const headerHeight = document.querySelector('.app-header')?.offsetHeight || 0;
      new IntersectionObserver(([entry]) => {
        chipWrap.classList.toggle('is-stuck', !entry.isIntersecting);
      }, { rootMargin: `-${headerHeight + 1}px 0px 0px` }).observe(sentinel);
    }
  }

  /* ---- watched state ----
     Every Mark watched row for the video turns into Mark unwatched and back, so the menu
     on a card and on the watch page always offers the opposite of the current state. */
  function flipWatchedRows(match, watched) {
    document.querySelectorAll(`[data-watched-toggle][data-video-id="${match}"]`).forEach((row) => {
      row.dataset.feedback = watched ? 'mark_unwatched' : 'mark_watched';
      const label = row.querySelector('.menu-label');
      if (label) label.textContent = watched ? 'Mark unwatched' : 'Mark watched';
    });
  }
  window.ofMarkUnwatched = (videoId) => {
    const match = window.CSS?.escape ? CSS.escape(videoId) : videoId;
    flipWatchedRows(match, false);
    document.querySelectorAll(`[data-video-card][data-video-id="${match}"]`).forEach((card) => {
      card.classList.remove('is-watched');
      card.querySelector('.watched-mark')?.remove();
      const quick = card.querySelector('.card-quick');
      if (!quick || quick.querySelector('[data-feedback="mark_watched"]')) return;
      const button = document.createElement('button');
      button.className = 'card-quick-button';
      button.type = 'button';
      button.dataset.feedback = 'mark_watched';
      button.dataset.videoId = videoId;
      button.setAttribute('aria-label', 'Mark watched');
      button.title = 'Mark watched';
      button.innerHTML = ICONS.check;
      quick.prepend(button);
    });
  };
  window.ofMarkWatched = (videoId) => {
    const match = window.CSS?.escape ? CSS.escape(videoId) : videoId;
    flipWatchedRows(match, true);
    document.querySelectorAll(`[data-video-card][data-video-id="${match}"]`).forEach((card) => {
      card.classList.add('is-watched');
      card.querySelector('.progress-rail')?.remove();
      const thumb = card.querySelector('.thumb');
      if (thumb && !thumb.querySelector('.watched-mark')) {
        thumb.insertAdjacentHTML('beforeend', `<span class="watched-mark" aria-hidden="true">${ICONS.check}</span>`);
      }
      const quick = card.querySelector('.card-quick [data-feedback="mark_watched"]');
      if (quick === document.activeElement) card.querySelector('h2 a, h3 a')?.focus({ preventScroll: true });
      quick?.remove();
    });
  };
})();
