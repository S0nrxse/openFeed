/* Captions, Media Session and casting for every player OpenFeed can reach into.

   Loaded by the player shell (/watch/{id}/player), never by the player documents
   themselves. The shell calls OpenFeedPlayerKit.attach() with the same-origin player's
   document and <video>: the Invidious embed (video.js) or the plain /local and /ytdlp
   pages (native.js). Both use video.js class names, so one set of controls fits both.
   Nothing here can reach a cross-origin YouTube embed, which keeps YouTube's own.

   Captions come from /api/captions/{id}: OpenFeed fetches every track server-side and
   serves it as WebVTT from this origin. The <track> elements run in "hidden" mode, so the
   browser parses cues and fires cuechange while the drawing is ours: one style in every
   player, lifted clear of the control bar. */
(() => {
  'use strict';

  const CAPTION_KEYS = new Set(['c', 'C']);

  function attach(options) {
    const { doc, video, videoId } = options;
    const win = doc.defaultView;
    const post = options.post || (() => {});
    const cleanups = [];
    const on = (target, name, handler, opts) => {
      target.addEventListener(name, handler, opts);
      cleanups.push(() => target.removeEventListener(name, handler, opts));
    };
    const root = video.closest('.video-js') || doc.body;
    const bar = doc.querySelector('.vjs-control-bar:not(.mobile-operations-bar)');
    let player = null;
    try {
      const library = win.videojs;
      player = library ? Object.values(library.getPlayers()).find(Boolean) : null;
    } catch (error) { player = null; }
    const context = { doc, win, video, videoId, root, bar, player, post, on };

    const captions = setupCaptions(context);
    const cast = setupCast(context);
    const session = setupMediaSession(context, options.meta || {}, options.queue || {});
    const keys = setupKeys(context);
    const quality = setupQuality(context);
    const autoplay = setupAutoplay(context, options.autoplay);
    setupSpeed(context, Number(options.speed) || 1);
    return {
      toggleCaptions: captions.toggle,
      setQueue: session.setQueue,
      /* The watch page's Autoplay state, for the switch on the bar. */
      setAutoplay: autoplay.set,
      /* A shortcut pressed while focus was on the watch page, forwarded by the shell. */
      key: keys.run,
      dispose() {
        session.dispose();
        cleanups.splice(0).forEach((cleanup) => { try { cleanup(); } catch (error) { /* frame already gone */ } });
        captions.dispose();
        cast.dispose();
        quality.dispose();
        autoplay.dispose();
      },
    };
  }

  /* --- Keyboard -------------------------------------------------------------------------
     One set of shortcuts wherever focus is in the player. video.js sends a key pressed on
     one of its controls (the scrubber after a click, a button) to the player's own hotkey
     hook and stops it there, and Invidious listens on the window, so after any click on
     the bar the keys did nothing. Caught on the way down instead, before either sees it.
     Invidious's own N calls a next_video it never defines in an embed; this one asks the
     watch page for its first Up next video. t and N belong to the page, so they go up. */
  const RATES = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];

  function typingIn(target) {
    const tag = String((target && target.tagName) || '').toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || Boolean(target && target.isContentEditable);
  }

  function setupKeys(context) {
    const { doc, win, video, root, player, post, on } = context;
    const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
    function seekBy(seconds) {
      const duration = Number(video.duration);
      const target = Math.max(0, (Number(video.currentTime) || 0) + seconds);
      video.currentTime = isFinite(duration) && duration > 0 ? Math.min(target, duration - 0.1) : target;
    }
    function seekToFraction(fraction) {
      const duration = Number(video.duration);
      if (isFinite(duration) && duration > 0) video.currentTime = duration * fraction;
    }
    function togglePlay() {
      if (video.paused || video.ended) {
        const started = video.play();
        if (started && started.catch) started.catch(() => {});
      } else {
        video.pause();
      }
    }
    function nudgeVolume(step) {
      video.muted = false;
      video.volume = clamp(Math.round((video.volume + step) * 100) / 100, 0, 1);
    }
    function stepRate(direction) {
      const now = Number(video.playbackRate) || 1;
      const index = RATES.findIndex((rate) => rate >= now - 0.001);
      const from = index < 0 ? RATES.length - 1 : index;
      video.playbackRate = RATES[clamp(from + direction, 0, RATES.length - 1)];
    }
    function toggleFullscreen() {
      try {
        if (player && typeof player.isFullscreen === 'function') {
          if (player.isFullscreen()) player.exitFullscreen(); else player.requestFullscreen();
          return;
        }
        if (doc.fullscreenElement) doc.exitFullscreen();
        else root.requestFullscreen?.();
      } catch (error) { /* fullscreen needs a gesture in this frame */ }
    }
    /* Returns true when the key was a shortcut and has been acted on. */
    function run(key) {
      switch (key) {
        case ' ': case 'k': case 'K': togglePlay(); return true;
        case 'j': case 'J': seekBy(-10); return true;
        case 'l': case 'L': seekBy(10); return true;
        case 'ArrowLeft': seekBy(-5); return true;
        case 'ArrowRight': seekBy(5); return true;
        case 'ArrowUp': nudgeVolume(0.1); return true;
        case 'ArrowDown': nudgeVolume(-0.1); return true;
        case 'm': case 'M': video.muted = !video.muted; return true;
        case 'f': case 'F': toggleFullscreen(); return true;
        case '>': stepRate(1); return true;
        case '<': stepRate(-1); return true;
        case ',': video.pause(); seekBy(-1 / 30); return true;
        case '.': video.pause(); seekBy(1 / 30); return true;
        case 'N': post({ type: 'openfeed:media-next' }); return true;
        case 'P': post({ type: 'openfeed:media-previous' }); return true;
        case 't': case 'T': post({ type: 'openfeed:theater-toggle' }); return true;
        default:
          if (/^[0-9]$/.test(key)) { seekToFraction(Number(key) / 10); return true; }
          return false;
      }
    }
    on(win, 'keydown', (event) => {
      if (event.ctrlKey || event.metaKey || event.altKey || event.defaultPrevented) return;
      const target = event.target;
      if (typingIn(target)) return;
      const key = event.key;
      /* What a focused control already does with a key stays its own: Space presses a
         button, and arrows move through an open menu or the volume slider. */
      const element = target instanceof win.Element ? target : null;
      if (key === ' ' && element?.closest('button, a[href], summary, [role="button"], [role^="menuitem"]')) return;
      if (key.startsWith('Arrow') && element?.closest('.vjs-menu, .vjs-menu-button, .of-cc, .vjs-volume-panel, [role="menu"]')) return;
      if (/^[0-9]$/.test(key) && String(event.code || '').startsWith('Numpad')) return;
      if (!run(key)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    }, true);
    return { run };
  }

  /* --- Quality ---------------------------------------------------------------------------
     Invidious's quality menu (videojs-http-source-selector) has an unnamed cog and never
     marks a choice: it compares rows against the rendition playing, by an index its own
     de-duplication has already shifted, and Auto can never match. The choice is read
     back from which renditions are enabled: all of them is Auto. */
  function setupQuality(context) {
    const { doc, win, video, bar, player, on } = context;
    const levels = player && typeof player.qualityLevels === 'function' ? player.qualityLevels() : null;
    if (!bar || !levels) return { dispose() {} };
    let pending = 0;
    function choice() {
      const all = Array.from({ length: levels.length }, (_, index) => levels[index]);
      const enabled = all.filter((level) => level.enabled);
      if (!all.length || enabled.length === all.length) return 'Auto';
      const heights = [...new Set(enabled.map((level) => level.height).filter(Boolean))];
      return heights.length === 1 ? `${heights[0]}p` : 'Auto';
    }
    function playing() {
      const level = levels[levels.selectedIndex];
      return level && level.height ? `${level.height}p` : '';
    }
    function paint() {
      pending = 0;
      const wrap = bar.querySelector(':scope > .vjs-http-source-selector');
      const button = wrap?.querySelector(':scope > button');
      if (!button) return;
      const chosen = choice();
      const now = playing();
      const label = chosen === 'Auto' && now ? `Quality: Auto, playing ${now}` : `Quality: ${chosen}`;
      if (button.getAttribute('aria-label') !== label) button.setAttribute('aria-label', label);
      if (button.dataset.tip !== 'Quality') button.dataset.tip = 'Quality';
      const text = button.querySelector('.vjs-control-text');
      if (text && text.textContent !== 'Quality') text.textContent = 'Quality';
      wrap.querySelectorAll('li.vjs-menu-item').forEach((item) => {
        const name = (item.querySelector('.vjs-menu-item-text') || item).textContent.trim();
        const selected = name === chosen;
        if (item.classList.contains('vjs-selected') !== selected) item.classList.toggle('vjs-selected', selected);
        if (item.getAttribute('aria-checked') !== String(selected)) item.setAttribute('aria-checked', String(selected));
        /* video.js also speaks ", selected" from a hidden span on the row it chose. */
        const spoken = item.querySelector('.vjs-control-text');
        const said = selected ? ', selected' : '';
        if (spoken && spoken.textContent !== said) spoken.textContent = said;
      });
    }
    /* After the plugin, which repaints its rows on the same events. */
    const later = () => { if (!pending) pending = win.setTimeout(paint, 0); };
    levels.on(['change', 'addqualitylevel', 'removequalitylevel'], later);
    on(bar, 'click', later);
    on(bar, 'keydown', later);
    on(video, 'loadedmetadata', later);
    const watcher = new win.MutationObserver(later);
    watcher.observe(bar, { childList: true, subtree: true });
    paint();
    return {
      dispose() {
        watcher.disconnect();
        try { levels.off(['change', 'addqualitylevel', 'removequalitylevel'], later); } catch (error) { /* player gone */ }
        if (pending) win.clearTimeout(pending);
      },
    };
  }

  function makeButton(doc, className, label) {
    const button = doc.createElement('button');
    button.type = 'button';
    button.className = `${className} vjs-control vjs-button`;
    button.setAttribute('aria-label', label);
    button.dataset.tip = label;
    const icon = doc.createElement('span');
    icon.className = 'vjs-icon-placeholder';
    icon.setAttribute('aria-hidden', 'true');
    button.appendChild(icon);
    return button;
  }

  /* Before the first of `selectors` that exists in the bar, else at its end. */
  function place(bar, node, selectors) {
    if (!bar) return false;
    for (const selector of selectors) {
      const anchor = bar.querySelector(`:scope > ${selector}`);
      if (anchor) { anchor.before(node); return true; }
    }
    bar.appendChild(node);
    return true;
  }

  /* --- Captions ------------------------------------------------------------------------ */

  function setupCaptions(context) {
    const { doc, win, video, videoId, root, bar, player, on } = context;
    let tracks = [];
    let preference = { enabled: false, language: '', auto: false };
    let active = null;
    let lastChoice = null;
    const elements = new Map();

    /* Invidious's own caption button reads the same tracks raw, word timings and doubled
       lines included, from its companion. Ours replace it rather than sit beside it. */
    root.classList.add('of-own-captions');
    if (player) {
      try { [...player.remoteTextTracks()].forEach((track) => player.removeRemoteTextTrack(track)); } catch (error) { /* nothing to remove */ }
    }

    const overlay = doc.createElement('div');
    overlay.className = 'of-captions';
    overlay.hidden = true;
    /* Before the bar in the DOM, so an open menu paints over a caption and not under it. */
    if (bar && bar.parentNode === root) root.insertBefore(overlay, bar);
    else root.appendChild(overlay);

    const wrap = doc.createElement('div');
    wrap.className = 'of-cc vjs-control vjs-menu-button vjs-menu-button-popup';
    wrap.hidden = true;
    const button = makeButton(doc, 'of-cc-control', 'Captions');
    button.setAttribute('aria-haspopup', 'menu');
    button.setAttribute('aria-expanded', 'false');
    const menu = doc.createElement('div');
    menu.className = 'vjs-menu of-cc-menu';
    const list = doc.createElement('ul');
    list.className = 'vjs-menu-content';
    list.setAttribute('role', 'menu');
    list.setAttribute('aria-label', 'Captions');
    menu.appendChild(list);
    wrap.append(button, menu);
    place(bar, wrap, ['.vjs-captions-button', '.vjs-subs-caps-button', '.vjs-subtitles-button', '.vjs-audio-button', '.vjs-playback-rate', '.of-pip-control', '.vjs-fullscreen-control']);

    const items = () => [...list.querySelectorAll('[role="menuitemradio"]')];
    function paintMenu() {
      items().forEach((item) => {
        const selected = (item.dataset.track || '') === (active ? active.id : '');
        item.classList.toggle('vjs-selected', selected);
        item.setAttribute('aria-checked', selected ? 'true' : 'false');
      });
      const label = active ? `Captions: ${active.label}${active.auto ? ', auto-generated' : ''}` : 'Captions off';
      button.setAttribute('aria-label', `${label}. Press c to toggle`);
      button.dataset.tip = active ? `Captions on (c)` : 'Captions (c)';
      button.classList.toggle('is-on', Boolean(active));
    }
    function buildMenu() {
      const rows = [{ id: '', label: 'Off', auto: false }, ...tracks].map((track) => {
        const item = doc.createElement('li');
        item.className = 'vjs-menu-item';
        item.setAttribute('role', 'menuitemradio');
        item.tabIndex = -1;
        item.dataset.track = track.id;
        const name = doc.createElement('span');
        name.className = 'of-cc-name';
        name.textContent = track.label;
        item.appendChild(name);
        if (track.auto) {
          const kind = doc.createElement('span');
          kind.className = 'of-cc-kind';
          kind.textContent = 'Auto-generated';
          item.appendChild(kind);
        }
        return item;
      });
      list.replaceChildren(...rows);
      paintMenu();
    }

    function render() {
      const track = active && elements.get(active.id);
      const cues = track ? [...(track.track.activeCues || [])] : [];
      overlay.replaceChildren(...cues.map((cue) => {
        const line = doc.createElement('span');
        line.className = 'of-cue';
        if (typeof cue.getCueAsHTML === 'function') line.appendChild(cue.getCueAsHTML());
        else line.textContent = cue.text || '';
        return line;
      }));
      overlay.hidden = !cues.length;
    }

    function element(track) {
      let node = elements.get(track.id);
      if (node) return node;
      node = doc.createElement('track');
      node.kind = 'captions';
      node.label = track.auto ? `${track.label} (auto-generated)` : track.label;
      if (track.language) node.srclang = track.language;
      node.src = track.src;
      node.addEventListener('error', () => {
        if (active && active.id === track.id) {
          overlay.replaceChildren(Object.assign(doc.createElement('span'), { className: 'of-cue of-cue-note', textContent: `${track.label} captions could not be loaded` }));
          overlay.hidden = false;
          win.setTimeout(render, 4000);
        }
      });
      node.addEventListener('load', render);
      video.appendChild(node);
      node.track.mode = 'disabled';
      node.track.addEventListener('cuechange', render);
      elements.set(track.id, node);
      return node;
    }

    /* iOS draws its own fullscreen player, where our overlay cannot follow, so the
       browser's renderer takes over there and hands back on the way out. */
    let nativeFullscreen = false;
    function applyModes() {
      elements.forEach((node, id) => {
        node.track.mode = active && active.id === id ? (nativeFullscreen ? 'showing' : 'hidden') : 'disabled';
      });
    }
    on(video, 'webkitbeginfullscreen', () => { nativeFullscreen = true; applyModes(); });
    on(video, 'webkitendfullscreen', () => { nativeFullscreen = false; applyModes(); render(); });

    function save(patch) {
      try {
        fetch('/api/playback/preferences', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) }).catch(() => {});
      } catch (error) { /* a preference is a convenience */ }
    }

    function choose(track, remember = true) {
      active = track || null;
      if (active) { lastChoice = active; element(active); }
      applyModes();
      paintMenu();
      render();
      if (remember) {
        save(active
          ? { captions_enabled: true, captions_language: active.language || '', captions_auto: Boolean(active.auto) }
          : { captions_enabled: false });
      }
    }

    /* The remembered language first, then the browser's own, manual before automatic. */
    function pick(language, auto) {
      const wanted = String(language || '').toLowerCase();
      if (!wanted) return null;
      const base = wanted.split('-')[0];
      const exact = tracks.filter((track) => String(track.language || '').toLowerCase() === wanted);
      const kind = exact.find((track) => Boolean(track.auto) === Boolean(auto));
      if (kind || exact.length) return kind || exact[0];
      const related = tracks.filter((track) => String(track.language || '').toLowerCase().split('-')[0] === base);
      return related.find((track) => !track.auto) || related[0] || null;
    }
    function toggle() {
      if (!tracks.length) return;
      if (active) { choose(null); return; }
      choose((lastChoice && tracks.includes(lastChoice) && lastChoice)
        || pick(preference.language, preference.auto)
        || pick(navigator.language || 'en', false)
        || tracks[0]);
    }

    /* --- the menu, by pointer and by keyboard --- */
    function open(focusSelected) {
      menu.classList.add('vjs-lock-showing');
      wrap.classList.add('is-open');
      button.setAttribute('aria-expanded', 'true');
      if (focusSelected) (items().find((item) => item.getAttribute('aria-checked') === 'true') || items()[0])?.focus();
    }
    function close(refocus) {
      menu.classList.remove('vjs-lock-showing');
      wrap.classList.remove('is-open');
      button.setAttribute('aria-expanded', 'false');
      if (refocus) button.focus();
    }
    on(button, 'click', (event) => {
      event.stopPropagation();
      if (menu.classList.contains('vjs-lock-showing')) close(false);
      else open(event.detail === 0);
    });
    on(button, 'keydown', (event) => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        event.stopPropagation();
        open(true);
      } else if (event.key === ' ' || event.key === 'Enter') {
        /* Invidious's page-wide Space is play/pause; this Space belongs to the button. */
        event.stopPropagation();
      } else if (event.key === 'Escape') {
        close(false);
      }
    });
    on(list, 'click', (event) => {
      const item = event.target.closest('[role="menuitemradio"]');
      if (!item) return;
      event.stopPropagation();
      choose(tracks.find((track) => track.id === item.dataset.track) || null);
      close(true);
    });
    on(list, 'keydown', (event) => {
      const all = items();
      const index = all.indexOf(doc.activeElement);
      const move = { ArrowDown: 1, ArrowUp: -1 }[event.key];
      event.stopPropagation();
      if (move) {
        event.preventDefault();
        all[(index + move + all.length) % all.length]?.focus();
      } else if (event.key === 'Home' || event.key === 'End') {
        event.preventDefault();
        all[event.key === 'Home' ? 0 : all.length - 1]?.focus();
      } else if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        doc.activeElement?.click();
      } else if (event.key === 'Escape') {
        event.preventDefault();
        close(true);
      } else if (event.key === 'Tab') {
        close(false);
      }
    });
    on(doc, 'click', (event) => { if (!wrap.contains(event.target)) close(false); });

    /* c toggles, wherever focus is in the player. Caught on the way down so Invidious's
       own c, which would switch its raw tracks back on, never sees it. */
    on(win, 'keydown', (event) => {
      if (!CAPTION_KEYS.has(event.key) || event.ctrlKey || event.metaKey || event.altKey) return;
      const tag = String(event.target && event.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || event.target?.isContentEditable) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      toggle();
    }, true);

    fetch(`/api/captions/${encodeURIComponent(videoId)}`)
      .then((response) => (response.ok ? response.json() : { tracks: [] }))
      .then((data) => {
        tracks = Array.isArray(data.tracks) ? data.tracks : [];
        preference = data.preference || preference;
        if (!tracks.length) return;
        wrap.hidden = false;
        buildMenu();
        /* Settings, Playback, "Always show captions": the remembered language, then the
           browser's, then the creator's own track before an automatic one. */
        if (preference.enabled) {
          const remembered = pick(preference.language, preference.auto)
            || pick(navigator.language || 'en', false)
            || tracks.find((track) => !track.auto) || tracks[0];
          if (remembered) choose(remembered, false);
        }
      })
      .catch(() => {});

    return {
      toggle,
      dispose() {
        elements.forEach((node) => node.remove());
        elements.clear();
        overlay.remove();
        wrap.remove();
      },
    };
  }

  /* --- Autoplay switch -------------------------------------------------------------------
     YouTube's place for it: on the bar, just before captions. The watch page owns the
     setting (it saves it and runs the countdown when a video ends), so the switch only
     shows once the page has said whether it is on, and a press is sent up to it. */
  function setupAutoplay(context, initial) {
    const { doc, bar, post, on } = context;
    if (!bar) return { set() {}, dispose() {} };
    const button = doc.createElement('button');
    button.type = 'button';
    button.className = 'of-autoplay-control vjs-control vjs-button';
    button.setAttribute('role', 'switch');
    button.setAttribute('aria-label', 'Autoplay');
    const track = doc.createElement('span');
    track.className = 'of-autoplay-track';
    track.setAttribute('aria-hidden', 'true');
    track.appendChild(doc.createElement('span')).className = 'of-autoplay-knob';
    button.appendChild(track);
    place(bar, button, ['.of-cc', '.vjs-captions-button', '.vjs-subs-caps-button', '.vjs-audio-button', '.vjs-playback-rate', '.of-pip-control', '.vjs-fullscreen-control']);
    let state = typeof initial === 'boolean' ? initial : null;
    function paint() {
      button.hidden = state === null;
      button.setAttribute('aria-checked', state ? 'true' : 'false');
      button.classList.toggle('is-on', Boolean(state));
      button.dataset.tip = state ? 'Autoplay is on' : 'Autoplay is off';
    }
    on(button, 'click', (event) => {
      event.stopPropagation();
      if (state === null) return;
      state = !state;
      paint();
      post({ type: 'openfeed:autoplay-set', on: state });
    });
    /* Invidious's page-wide Space is play/pause; this Space belongs to the switch. */
    on(button, 'keydown', (event) => { if (event.key === ' ' || event.key === 'Enter') event.stopPropagation(); });
    paint();
    return {
      set(value) { state = Boolean(value); paint(); },
      dispose() { button.remove(); },
    };
  }

  /* --- Default speed -----------------------------------------------------------------------
     Settings, Playback, "Default speed": applied once as the video starts. The speed menu
     still changes it for this video, and nothing here sets it back. Through video.js when
     there is one, so its menu marks the speed as chosen. */
  function setupSpeed(context, speed) {
    const { video, player, on } = context;
    if (!(speed > 0) || speed === 1) return;
    let applied = 0;
    function apply() {
      applied += 1;
      try {
        if (player && typeof player.playbackRate === 'function') player.playbackRate(speed);
        else video.playbackRate = speed;
        video.defaultPlaybackRate = speed;
      } catch (error) { /* the player is going away */ }
    }
    if (video.readyState >= 1) apply();
    else on(video, 'loadedmetadata', () => { if (!applied) apply(); }, { once: true });
    /* Invidious sets its own saved speed as playback starts; if that undid this one, once. */
    on(video, 'playing', () => { if (applied === 1 && Math.abs(video.playbackRate - 1) < 0.001) apply(); }, { once: true });
  }

  /* --- Cast: the browser's own Remote Playback, never Google's Cast SDK --------------- */

  function setupCast(context) {
    const { doc, win, video, bar, on } = context;
    const button = makeButton(doc, 'of-cast-control', 'Play on a TV');
    button.hidden = true;
    place(bar, button, ['.vjs-fullscreen-control']);
    const remote = video.remote;
    const airplay = Boolean(win.WebKitPlaybackTargetAvailabilityEvent);
    let remoteAvailable = false;
    let airplayAvailable = false;
    let watchId = null;

    /* A MediaSource (blob:) stream, which is how the Invidious DASH player plays, cannot be
       handed to a receiver. A plain URL from this server can. */
    const castable = () => {
      const src = String(video.currentSrc || video.src || '');
      return Boolean(src) && !src.startsWith('blob:') && !video.disableRemotePlayback;
    };
    const sync = () => {
      button.hidden = !((remoteAvailable || airplayAvailable) && castable());
    };
    function paintState() {
      const state = remote ? remote.state : 'disconnected';
      button.classList.toggle('is-on', state === 'connected');
      button.classList.toggle('is-connecting', state === 'connecting');
      const label = state === 'connected' ? 'Playing on a TV' : state === 'connecting' ? 'Connecting to a TV' : airplay ? 'AirPlay' : 'Play on a TV';
      button.setAttribute('aria-label', label);
      button.dataset.tip = label;
    }

    if (airplay) {
      video.setAttribute('x-webkit-airplay', 'allow');
      on(video, 'webkitplaybacktargetavailabilitychanged', (event) => {
        airplayAvailable = event.availability === 'available';
        sync();
      });
    }
    if (remote && typeof remote.watchAvailability === 'function') {
      remote.watchAvailability((value) => { remoteAvailable = value; sync(); })
        .then((id) => { watchId = id; })
        .catch(() => { /* NotSupportedError: this source cannot be cast */ remoteAvailable = false; sync(); });
      ['connecting', 'connect', 'disconnect'].forEach((name) => on(remote, name, paintState));
    }
    ['loadedmetadata', 'emptied', 'loadstart'].forEach((name) => on(video, name, sync));
    on(button, 'click', () => {
      if (remote && typeof remote.prompt === 'function') {
        remote.prompt().catch((error) => {
          /* Cancelling the picker is not an error worth saying out loud. */
          if (error && error.name !== 'AbortError' && error.name !== 'NotAllowedError') {
            button.dataset.tip = error.name === 'NotFoundError' ? 'No TV found' : 'Cannot cast this source';
          }
        });
      } else if (typeof video.webkitShowPlaybackTargetPicker === 'function') {
        video.webkitShowPlaybackTargetPicker();
      }
    });
    on(button, 'keydown', (event) => { if (event.key === ' ' || event.key === 'Enter') event.stopPropagation(); });
    paintState();
    sync();
    return {
      dispose() {
        if (remote && watchId !== null) remote.cancelWatchAvailability(watchId).catch(() => {});
        button.remove();
      },
    };
  }

  /* --- Media Session: lock screen, notification and headphone controls ------------------ */

  function setupMediaSession(context, meta, initialQueue) {
    const { win, video, videoId, post, on } = context;
    const session = win.navigator && win.navigator.mediaSession;
    if (!session) return { setQueue() {}, dispose() {} };
    let queue = initialQueue || {};
    const artwork = [['mqdefault.jpg', '320x180'], ['hqdefault.jpg', '480x360']].map(([file, sizes]) => ({
      src: new URL(`/vi/${encodeURIComponent(videoId)}/${file}`, win.location.href).href,
      sizes,
      type: 'image/jpeg',
    }));
    function setMetadata() {
      if (typeof win.MediaMetadata !== 'function') return;
      try {
        session.metadata = new win.MediaMetadata({ title: meta.title || videoId, artist: meta.channel || '', album: 'OpenFeed', artwork });
      } catch (error) { /* metadata is decoration */ }
    }
    function position() {
      if (typeof session.setPositionState !== 'function') return;
      const duration = Number(video.duration);
      try {
        if (!isFinite(duration) || duration <= 0) { session.setPositionState(); return; }
        session.setPositionState({ duration, playbackRate: video.playbackRate || 1, position: Math.min(Math.max(0, Number(video.currentTime) || 0), duration) });
      } catch (error) { /* an out-of-range reading mid-seek; the next event corrects it */ }
    }
    function seekBy(seconds) {
      const duration = Number(video.duration);
      const target = Math.max(0, (Number(video.currentTime) || 0) + seconds);
      video.currentTime = isFinite(duration) && duration > 0 ? Math.min(target, duration - 0.1) : target;
      position();
    }
    const set = (action, handler) => { try { session.setActionHandler(action, handler); } catch (error) { /* unsupported action */ } };
    function bind() {
      set('play', () => { const started = video.play(); if (started && started.catch) started.catch(() => {}); });
      set('pause', () => video.pause());
      set('seekbackward', (details) => seekBy(-((details && details.seekOffset) || 10)));
      set('seekforward', (details) => seekBy((details && details.seekOffset) || 10));
      set('seekto', (details) => {
        if (!details || !isFinite(details.seekTime)) return;
        if (details.fastSeek && typeof video.fastSeek === 'function') video.fastSeek(details.seekTime);
        else video.currentTime = details.seekTime;
        position();
      });
      /* Previous restarts the video, or goes back to the one before it when this one has
         barely started and there is one to go back to: the convention music players use. */
      set('previoustrack', () => {
        if ((Number(video.currentTime) || 0) > 5 || !queue.previous) { video.currentTime = 0; position(); return; }
        post({ type: 'openfeed:media-previous' });
      });
      set('nexttrack', queue.next ? () => post({ type: 'openfeed:media-next' }) : null);
    }
    setMetadata();
    bind();
    on(video, 'play', () => { session.playbackState = 'playing'; setMetadata(); position(); });
    on(video, 'pause', () => { session.playbackState = 'paused'; position(); });
    ['loadedmetadata', 'durationchange', 'ratechange', 'seeked'].forEach((name) => on(video, name, position));
    let lastSync = 0;
    on(video, 'timeupdate', () => {
      const now = Date.now();
      if (now - lastSync > 10000) { lastSync = now; position(); }
    });
    if (!video.paused) session.playbackState = 'playing';
    return {
      setQueue(next) { queue = next || {}; bind(); },
      dispose() {
        ['play', 'pause', 'seekbackward', 'seekforward', 'seekto', 'previoustrack', 'nexttrack'].forEach((action) => set(action, null));
        try { session.metadata = null; } catch (error) { /* frame gone */ }
      },
    };
  }

  window.OpenFeedPlayerKit = { attach };
})();
