/* Shared UI behaviour: image loading, range readouts, toast, confirm, bottom sheets and
   the search shortcut. Runs first in the shell bundle, so everything after it can use
   window.ofToast and window.ofConfirm. No markup is assumed beyond the toast region. */
(() => {
  'use strict';

  /* ---- image loading ----
     load and error do not bubble, but they do capture, so one listener covers every
     image however it got here: server-rendered, feed pagination, or built by script.
     Images that finished before this ran are marked in the same task that sets
     of-ready, which is why a cached thumbnail never fades. */
  document.addEventListener('load', (event) => {
    if (!(event.target instanceof HTMLImageElement)) return;
    event.target.classList.remove('is-failed');
    event.target.classList.add('is-loaded');
  }, true);
  document.addEventListener('error', (event) => {
    if (event.target instanceof HTMLImageElement) event.target.classList.add('is-failed');
  }, true);
  document.querySelectorAll('img').forEach((img) => {
    if (!img.complete) return;
    img.classList.add(img.naturalWidth ? 'is-loaded' : 'is-failed');
  });
  document.documentElement.classList.add('of-ready');

  /* ---- list loaders ----
     A loader sits below the rows, so hiding it shortens the page. When the viewport was
     on it, the browser then pulls the page up to the new bottom with no input from the
     viewer. The height it gave up is held until the viewport moves away, and handed back
     as they scroll up, so the document always ends at the bottom of the screen. */
  window.ofHideLoader = (loader) => {
    if (!loader || loader.hidden) return;
    const root = document.documentElement;
    const top = window.scrollY;
    loader.hidden = true;
    const target = top + window.innerHeight;
    if (target <= root.scrollHeight) return;
    const hold = document.createElement('div');
    hold.setAttribute('aria-hidden', 'true');
    /* A page shorter than the screen still reports the screen's height, so the height
       needed cannot be read off it. Overshooting first shows how far the content reaches. */
    hold.style.height = `${target}px`;
    loader.after(hold);
    hold.style.height = `${2 * target - root.scrollHeight}px`;
    window.scrollTo({ top, behavior: 'instant' });
    const release = () => {
      const below = root.scrollHeight - (window.scrollY + window.innerHeight);
      const left = hold.offsetHeight - below;
      if (left > 0) { hold.style.height = `${left}px`; return; }
      hold.remove();
      window.removeEventListener('scroll', release);
    };
    window.addEventListener('scroll', release, { passive: true });
  };

  /* ---- navigation feedback ----
     A page can take seconds to answer and the browser's own indicator may be out of
     sight, so anything that has not finished after 250ms draws a thin line under the
     header. These listeners sit on window because a handler on document that cancels the
     navigation has to have run first. */
  const NAV_DELAY = 250;
  const NAV_GIVE_UP = 30000;
  let navLine = null;
  let navTimers = [];
  function navDone() {
    navTimers.forEach((timer) => window.clearTimeout(timer));
    navTimers = [];
    if (navLine) navLine.hidden = true;
  }
  window.ofNavPending = () => {
    navDone();
    navTimers = [
      window.setTimeout(() => {
        if (!navLine) {
          navLine = document.createElement('div');
          navLine.className = 'nav-line';
          navLine.setAttribute('aria-hidden', 'true');
          document.body.appendChild(navLine);
        }
        navLine.hidden = false;
      }, NAV_DELAY),
      /* A response that turns out not to be a page never unloads this one. */
      window.setTimeout(navDone, NAV_GIVE_UP),
    ];
  };
  /* The same URL with only a fragment added stays on the page, and /api/ answers are
     files or JSON rather than pages. */
  function startsNavigation(url) {
    if (url.origin !== window.location.origin || url.pathname.startsWith('/api/')) return false;
    return !(url.hash && url.pathname === window.location.pathname && url.search === window.location.search);
  }
  window.addEventListener('click', (event) => {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.('a[href]');
    if (!link || link.hasAttribute('download') || (link.target && link.target !== '_self')) return;
    if (startsNavigation(new URL(link.href, window.location.href))) window.ofNavPending();
  });
  window.addEventListener('submit', (event) => {
    const form = event.target;
    if (event.defaultPrevented || form.method !== 'get' || (form.target && form.target !== '_self')) return;
    if (startsNavigation(new URL(form.action, window.location.href))) window.ofNavPending();
  });
  /* pagehide keeps the line out of the bfcache snapshot, pageshow covers a page restored
     from it anyway. */
  window.addEventListener('pagehide', navDone);
  window.addEventListener('pageshow', navDone);

  /* ---- range ---- */
  function syncRange(input) {
    const min = Number(input.min || 0);
    const max = Number(input.max || 100);
    const percent = max > min ? ((Number(input.value) - min) / (max - min)) * 100 : 0;
    input.style.setProperty('--fill', `${percent}%`);
    const output = input.closest('.range-field')?.querySelector('output');
    if (output) output.textContent = `${input.value}${output.dataset.suffix || ''}`;
  }
  document.querySelectorAll('input.range, input.fc-slider').forEach(syncRange);
  document.addEventListener('input', (event) => {
    if (event.target.matches?.('input.range, input.fc-slider')) syncRange(event.target);
  });

  /* ---- toast ---- */
  const MAX_TOASTS = 3;
  const CLOSE_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';

  /* Re-showing moves the region to the top of the top layer, above any dialog or sheet
     opened since. Where popover is missing it is still an ordinary fixed element. */
  function raiseToasts(region) {
    if (typeof region.showPopover !== 'function') return;
    if (region.matches(':popover-open')) region.hidePopover();
    region.showPopover();
  }

  /* kind: 'info' (default), 'success' or 'error'. Use 'success' only for a real backend
     acknowledgement, because it carries the green status dot. duration is in ms and 0
     means stay until dismissed, which is the default for errors. */
  window.ofToast = (message, options = {}) => {
    const region = document.querySelector('[data-toast-region]');
    if (!region) return { dismiss() {} };
    const { kind = 'info', action, actionLabel, duration } = options;
    const hasAction = typeof action === 'function' && Boolean(actionLabel);
    const lifetime = duration ?? (kind === 'error' ? 0 : hasAction ? 6000 : 4000);

    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.dataset.kind = kind;
    toast.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    const dot = document.createElement('span');
    dot.className = 'toast-dot';
    dot.setAttribute('aria-hidden', 'true');
    const text = document.createElement('span');
    text.className = 'toast-message';
    text.textContent = message;
    toast.append(dot, text);

    let timer = 0;
    const dismiss = () => {
      window.clearTimeout(timer);
      toast.remove();
      if (!region.children.length && region.matches?.(':popover-open')) region.hidePopover();
    };
    if (hasAction) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'toast-action';
      button.textContent = actionLabel;
      button.addEventListener('click', (event) => { dismiss(); action(event); }, { once: true });
      toast.append(button);
    }
    if (!lifetime) {
      const close = document.createElement('button');
      close.type = 'button';
      close.className = 'toast-close';
      close.setAttribute('aria-label', 'Dismiss');
      close.innerHTML = CLOSE_ICON;
      close.addEventListener('click', dismiss, { once: true });
      toast.append(close);
    }
    while (region.children.length >= MAX_TOASTS) region.firstElementChild.remove();
    region.append(toast);
    raiseToasts(region);
    if (lifetime) timer = window.setTimeout(dismiss, lifetime);
    return { dismiss };
  };

  /* ---- confirm ---- */
  let confirmDialog = null;
  let confirmParts = null;
  let settleConfirm = null;

  function buildConfirm() {
    confirmDialog = document.createElement('dialog');
    confirmDialog.className = 'feed-dialog confirm-dialog';
    confirmDialog.setAttribute('aria-labelledby', 'of-confirm-title');
    confirmDialog.innerHTML = `
      <form class="confirm-form" method="dialog">
        <h2 id="of-confirm-title"></h2>
        <p data-confirm-body></p>
        <label class="confirm-typed" data-confirm-typed hidden>
          <span>Type <code data-confirm-phrase></code> to confirm</span>
          <input class="field-input" type="text" autocomplete="off" autocapitalize="off" spellcheck="false">
        </label>
        <div class="confirm-actions">
          <button class="btn" type="submit" value="cancel" autofocus>Cancel</button>
          <button class="btn" type="submit" value="confirm" data-confirm-accept></button>
        </div>
      </form>`;
    document.body.append(confirmDialog);
    confirmParts = {
      title: confirmDialog.querySelector('h2'),
      body: confirmDialog.querySelector('[data-confirm-body]'),
      typed: confirmDialog.querySelector('[data-confirm-typed]'),
      phrase: confirmDialog.querySelector('[data-confirm-phrase]'),
      input: confirmDialog.querySelector('input'),
      accept: confirmDialog.querySelector('[data-confirm-accept]'),
    };
    /* A stale close event from a superseded call lands while the dialog is open again. */
    confirmDialog.addEventListener('close', () => {
      if (confirmDialog.open || !settleConfirm) return;
      const settle = settleConfirm;
      settleConfirm = null;
      settle(confirmDialog.returnValue === 'confirm');
    });
    /* Enter must not fall through to Cancel, the form's first submit button. */
    confirmParts.input.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      if (!confirmParts.accept.disabled) confirmDialog.querySelector('form').requestSubmit(confirmParts.accept);
    });
  }

  /* Resolves true only if the person confirmed. danger: true is Ember Text on Graphite;
     danger: 'filled' is the one Ember fill, for an irreversible wipe. typed: a phrase
     that must be entered exactly before the confirm button enables. */
  window.ofConfirm = ({ title = 'Are you sure?', body = '', confirmLabel = 'Confirm', danger = false, typed = '' } = {}) => {
    if (!confirmDialog) buildConfirm();
    /* The menu that asked has done its job, and at phone width it would show as a second
       sheet behind this one. */
    document.querySelectorAll('.yt-menu[open]').forEach((menu) => menu.removeAttribute('open'));
    if (settleConfirm) {
      settleConfirm(false);
      settleConfirm = null;
      confirmDialog.close();
    }
    const { title: heading, body: copy, typed: typedRow, phrase, input, accept } = confirmParts;
    heading.textContent = title;
    copy.textContent = body;
    copy.hidden = !body;
    accept.textContent = confirmLabel;
    accept.className = `btn ${danger === 'filled' ? 'btn-danger-fill' : danger ? 'btn-danger' : 'btn-primary'}`;
    typedRow.hidden = !typed;
    phrase.textContent = typed;
    input.value = '';
    accept.disabled = Boolean(typed);
    input.oninput = typed ? () => { accept.disabled = input.value !== typed; } : null;
    confirmDialog.returnValue = '';
    return new Promise((resolve) => {
      settleConfirm = resolve;
      confirmDialog.showModal();
    });
  };

  /* ---- bottom sheets ----
     At phone width every .yt-menu and .feed-dialog is a sheet. One observer on the open
     attribute covers every way these open and close, so call sites need no changes.

     Menu panels are promoted to the top layer with popover rather than moved: the DOM
     stays where the click handlers expect it, and neither .app-header's backdrop-filter
     nor a scrolling shelf can trap or clip the panel.

     The back gesture needs a history entry. Closing never calls history.back(), which
     could cancel a reload or navigation the closing action just started. The entry stays
     and the next sheet reuses it; if Back lands on it with nothing open, the popstate
     handler steps past it so that press still does what the person meant. */
  const phone = window.matchMedia('(max-width: 600px)');
  const sheets = [];
  let entryArmed = false;

  const isSheetHost = (element) => element.matches('details.yt-menu, dialog.feed-dialog');
  const panelOf = (host) => (host.matches('dialog') ? host : host.querySelector(':scope > .menu-panel'));

  function armHistory() {
    entryArmed = true;
    if (!history.state?.ofSheet) history.pushState({ ofSheet: true }, '');
  }

  function openSheet(host) {
    const panel = panelOf(host);
    if (!panel || sheets.includes(host)) return;
    sheets.push(host);
    rememberOpener(host);
    if (!panel.querySelector(':scope > .sheet-handle')) {
      const handle = document.createElement('div');
      handle.className = 'sheet-handle';
      handle.setAttribute('aria-hidden', 'true');
      panel.prepend(handle);
    }
    if (panel !== host && typeof panel.showPopover === 'function') {
      panel.setAttribute('popover', 'manual');
      panel.showPopover();
      panel.querySelector('a[href], button:not(:disabled)')?.focus({ preventScroll: true });
    }
    document.documentElement.classList.add('of-sheet-open');
    armHistory();
  }

  function forgetSheet(host) {
    const index = sheets.indexOf(host);
    if (index < 0) return;
    sheets.splice(index, 1);
    const panel = panelOf(host);
    panel?.style.removeProperty('transform');
    if (panel && panel !== host && panel.hasAttribute('popover')) {
      const hadFocus = panel.contains(document.activeElement);
      if (panel.matches(':popover-open')) panel.hidePopover();
      panel.removeAttribute('popover');
      if (hadFocus) returnFocus(host);
    }
    if (!sheets.length) document.documentElement.classList.remove('of-sheet-open');
  }

  function dismissSheet(host) {
    if (host.matches('dialog')) host.close();
    else host.removeAttribute('open');
    forgetSheet(host);
  }

  new MutationObserver((records) => {
    for (const { type, target } of records) {
      if (type !== 'attributes' || !isSheetHost(target)) continue;
      if (!target.hasAttribute('open')) forgetSheet(target);
      else if (phone.matches) openSheet(target);
    }
    /* A card can be deleted with its menu still open. */
    sheets.filter((host) => !host.isConnected).forEach(forgetSheet);
  }).observe(document.documentElement, { subtree: true, childList: true, attributes: true, attributeFilter: ['open'] });

  /* Rotating a phone must not throw away a half-filled dialog, so only menus close. */
  phone.addEventListener('change', () => {
    if (phone.matches) {
      document.querySelectorAll('dialog.feed-dialog[open]').forEach(openSheet);
    } else {
      [...sheets].forEach((host) => (host.matches('dialog') ? forgetSheet(host) : dismissSheet(host)));
    }
  });

  window.addEventListener('popstate', (event) => {
    if (event.state?.ofSheet) return;
    const top = sheets.at(-1);
    if (top) {
      /* A sheet with sub-screens can take the press to step back one level instead. */
      if (!top.dispatchEvent(new Event('sheet-back', { cancelable: true }))) {
        armHistory();
        return;
      }
      dismissSheet(top);
      if (sheets.length) armHistory();
      else entryArmed = false;
    } else if (entryArmed) {
      entryArmed = false;
      history.back();
    }
  });

  /* A tap on a modal dialog's ::backdrop reports the dialog itself as its target, from
     outside its box. A popover's ::backdrop takes no input at all, so a tap above a menu
     sheet lands on whatever the page has there: a card, a link, the header. Under a menu
     sheet the page is dimmed and not meant to be live, so any tap outside the panel only
     closes the sheet. Toasts stay usable: they sit above the sheet. Registered on window
     so it runs before every page handler, including the navigation feedback. */
  window.addEventListener('click', (event) => {
    const top = sheets.at(-1);
    if (!top) return;
    const panel = panelOf(top);
    if (panel === top) {
      if (event.target !== panel || event.clientY >= panel.getBoundingClientRect().top) return;
    } else if (panel.contains(event.target) || event.target.closest?.('[data-toast-region], dialog[open]')) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    dismissSheet(top);
  }, true);

  document.addEventListener('pointerdown', (event) => {
    const handle = event.target.closest?.('.sheet-handle');
    const host = handle && sheets.find((item) => panelOf(item) === handle.parentElement);
    if (!host) return;
    const panel = handle.parentElement;
    const startY = event.clientY;
    const startTime = event.timeStamp;
    let distance = 0;
    handle.setPointerCapture(event.pointerId);
    panel.classList.add('is-dragging');
    const move = (moveEvent) => {
      distance = Math.max(0, moveEvent.clientY - startY);
      panel.style.transform = `translateY(${distance}px)`;
    };
    const finish = (endEvent) => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', finish);
      handle.removeEventListener('pointercancel', finish);
      panel.classList.remove('is-dragging');
      panel.style.removeProperty('transform');
      const speed = distance / Math.max(1, endEvent.timeStamp - startTime);
      if (endEvent.type === 'pointerup' && (distance > 80 || speed > 0.5)) dismissSheet(host);
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', finish);
    handle.addEventListener('pointercancel', finish);
  });

  /* ---- menu and dialog focus ----
     Menus are details elements. Closing one hides the panel, and a focused row inside it
     would drop focus to <body>, so Escape hands focus back to what opened the menu first:
     the summary, or the You tab that opens the account sheet. While a menu or a modal
     dialog is open, Tab and Shift+Tab cycle inside it. A native modal dialog alone lets
     Tab step out to the browser between its last control and its first. */
  const FOCUSABLE = 'a[href], button, input:not([type="hidden"]), select, textarea, summary, iframe, [tabindex]';
  const menuOpeners = new WeakMap();

  function tabStops(root) {
    return [...root.querySelectorAll(FOCUSABLE)].filter((element) => !element.disabled
      && element.tabIndex >= 0
      && !element.closest('[inert]')
      && (element.checkVisibility ? element.checkVisibility({ visibilityProperty: true }) : element.getClientRects().length > 0));
  }

  function menuStops(menu) {
    const summary = menu.querySelector(':scope > summary');
    const panel = menu.querySelector(':scope > .menu-panel');
    const stops = panel ? tabStops(panel) : [];
    return summary && summary.getClientRects().length ? [summary, ...stops] : stops;
  }

  function openMenuWithFocus() {
    const active = document.activeElement;
    const menus = [...document.querySelectorAll('.yt-menu[open]')];
    return menus.find((menu) => menu.contains(active)) || null;
  }

  /* Called from the toggle event, and earlier from openSheet, which moves focus into the
     sheet before the toggle event is delivered. */
  function rememberOpener(menu) {
    const active = document.activeElement;
    if (active && active !== document.body && !menu.contains(active)) menuOpeners.set(menu, active);
  }
  document.addEventListener('toggle', (event) => {
    const menu = event.target;
    if (!(menu instanceof HTMLDetailsElement) || !menu.matches('.yt-menu')) return;
    if (menu.open) rememberOpener(menu);
    else window.setTimeout(() => { if (!menu.open) menuOpeners.delete(menu); });
  }, true);

  function returnFocus(menu) {
    const opener = menuOpeners.get(menu);
    const summary = menu.querySelector(':scope > summary');
    const target = opener?.isConnected && opener.getClientRects().length ? opener : summary;
    target?.focus({ preventScroll: true });
  }

  /* Capture, so focus is placed before the page script's own Escape handler closes every menu. */
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    if (document.activeElement?.closest?.('dialog[open]')) return;
    const menu = openMenuWithFocus() || [...document.querySelectorAll('.yt-menu[open]')].at(-1);
    if (!menu) return;
    /* Focus somewhere else, such as the search field, stays where it is. */
    const active = document.activeElement;
    const lost = !active || active === document.body || menu.contains(active);
    menu.removeAttribute('open');
    if (lost) returnFocus(menu);
  }, true);

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab' || event.ctrlKey || event.metaKey || event.altKey || event.defaultPrevented) return;
    const active = document.activeElement;
    const dialog = active?.closest?.('dialog[open]')
      || [...document.querySelectorAll('dialog[open]')].filter((item) => item.matches(':modal')).at(-1);
    let stops;
    let container;
    if (dialog && dialog.matches(':modal')) {
      container = dialog;
      stops = tabStops(dialog);
    } else {
      container = openMenuWithFocus();
      if (!container) return;
      stops = menuStops(container);
    }
    if (!stops.length) return;
    const index = stops.indexOf(active);
    let next = null;
    /* Focus inside on something that is not a stop (a heading focused on open) moves on natively. */
    if (index < 0 && container.contains(active)) return;
    if (index < 0) next = event.shiftKey ? stops.at(-1) : stops[0];
    else if (event.shiftKey && index === 0) next = stops.at(-1);
    else if (!event.shiftKey && index === stops.length - 1) next = stops[0];
    if (!next) return;
    event.preventDefault();
    next.focus();
  });

  /* ---- keyboard ---- */
  document.addEventListener('keydown', (event) => {
    if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey || event.defaultPrevented) return;
    if (event.target.closest?.('input, textarea, select, [contenteditable]')) return;
    if (document.querySelector('dialog[open]')) return;
    const search = document.querySelector('.search-form input');
    /* The header field is display:none on phone widths, and focus() would do nothing. */
    if (!search || !search.offsetParent) return;
    event.preventDefault();
    search.focus();
    search.select();
  });

  /* ---- keyboard shortcuts help ----
     ? (Shift+/) lists the keys this page really answers to. Each list mirrors a handler:
     the "/" one above, theater mode and the Shorts deck in the page script, and the
     Invidious player's own hotkeys, which only reach the player once it has focus because
     it is a separate document in an iframe. */
  const SHORTCUTS = {
    global: [
      [['/'], 'Search'],
      [['?'], 'Show keyboard shortcuts'],
      [['Esc'], 'Close a menu, sheet or dialog'],
    ],
    watch: [
      [['t'], 'Theater mode'],
    ],
    player: [
      [['Space', 'k'], 'Play or pause'],
      [['j', 'l'], 'Back or forward 10 seconds'],
      [['←', '→'], 'Back or forward 5 seconds'],
      [['↑', '↓'], 'Volume up or down'],
      [['m'], 'Mute'],
      [['f'], 'Full screen'],
      [['c'], 'Captions'],
      [['0', '9'], 'Jump to 0% to 90% of the video', 'to'],
      [['<', '>'], 'Slower or faster'],
      [[',', '.'], 'Previous or next frame, while paused'],
    ],
    shorts: [
      [['↓', 'j'], 'Next Short'],
      [['↑'], 'Previous Short'],
      [['Space', 'k'], 'Play or pause'],
      [['m'], 'Mute'],
      [['c'], 'Comments'],
    ],
  };
  let shortcutsDialog = null;

  function shortcutGroup(title, rows, note = '') {
    const section = document.createElement('section');
    section.className = 'shortcut-group';
    const heading = document.createElement('h3');
    heading.textContent = title;
    section.append(heading);
    if (note) {
      const caption = document.createElement('p');
      caption.className = 'shortcut-note';
      caption.textContent = note;
      section.append(caption);
    }
    const list = document.createElement('dl');
    list.className = 'shortcut-list';
    rows.forEach(([keys, label, joiner = 'or']) => {
      const row = document.createElement('div');
      const term = document.createElement('dt');
      keys.forEach((key, index) => {
        if (index) term.append(` ${joiner} `);
        const kbd = document.createElement('kbd');
        kbd.textContent = key;
        term.append(kbd);
      });
      const description = document.createElement('dd');
      description.textContent = label;
      row.append(term, description);
      list.append(row);
    });
    section.append(list);
    return section;
  }

  function openShortcuts() {
    if (!shortcutsDialog) {
      shortcutsDialog = document.createElement('dialog');
      shortcutsDialog.className = 'feed-dialog shortcuts-dialog';
      shortcutsDialog.setAttribute('aria-labelledby', 'of-shortcuts-title');
      shortcutsDialog.innerHTML = `
        <form method="dialog" class="dialog-head">
          <h2 id="of-shortcuts-title">Keyboard shortcuts</h2>
          <button class="icon-button" type="submit" aria-label="Close">${CLOSE_ICON}</button>
        </form>
        <div class="shortcuts-body" data-shortcuts-body tabindex="0" role="region" aria-label="Shortcut list"></div>`;
      document.body.append(shortcutsDialog);
    }
    const body = shortcutsDialog.querySelector('[data-shortcuts-body]');
    const search = document.querySelector('.search-form input');
    const global = SHORTCUTS.global.filter(([keys]) => keys[0] !== '/' || (search && search.offsetParent));
    const groups = [shortcutGroup('Everywhere', global)];
    if (document.querySelector('[data-shorts-stage]')) {
      groups.push(shortcutGroup('Shorts', SHORTCUTS.shorts));
    } else if (document.querySelector('[data-watch-layout]')) {
      if (document.querySelector('[data-theater-toggle]')) groups.push(shortcutGroup('Watch page', SHORTCUTS.watch));
      groups.push(shortcutGroup('Player', SHORTCUTS.player, 'Click the video or Tab into it first: the player takes these keys while it has focus.'));
    }
    body.replaceChildren(...groups);
    document.querySelectorAll('.yt-menu[open]').forEach((menu) => menu.removeAttribute('open'));
    shortcutsDialog.showModal();
    shortcutsDialog.querySelector('button')?.focus();
  }

  document.addEventListener('keydown', (event) => {
    if (event.key !== '?' || event.ctrlKey || event.metaKey || event.altKey || event.defaultPrevented) return;
    if (event.target.closest?.('input, textarea, select, [contenteditable]')) return;
    if (shortcutsDialog?.open) {
      event.preventDefault();
      shortcutsDialog.close();
      return;
    }
    if (document.querySelector('dialog[open]')) return;
    event.preventDefault();
    openShortcuts();
  });
})();
