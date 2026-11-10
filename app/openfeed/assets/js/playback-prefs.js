/* Playback preferences on the page side.

   - Autoplay: when a video ends with Autoplay on, a countdown card over the player names
     the first Up next video, with Cancel and Play now, then opens it. A list played from
     Play all or Shuffle (?list=) moves to its own next item instead (main.py, "list
     playback"), and Shorts never autoplay, nor are autoplayed into. The switch lives on the
     player bar (assets/player/kit/media-kit.js) and in Settings, Playback; both save the
     same server-side preference, and the page is rendered with it, so nothing flashes.
   - Share: the watch page's Share dialog, with "Start at m:ss" from the player's clock.
   - Settings, Playback, Player: autoplay, default speed and captions, saved on change. */
(() => {
  'use strict';

  const frame = () => document.querySelector('.player-frame');
  const tell = (message) => {
    const player = frame();
    if (player && player.contentWindow) player.contentWindow.postMessage(message, window.location.origin);
  };
  const fromPlayer = (event) => event.origin === window.location.origin && Boolean(frame()) && event.source === frame().contentWindow;
  const toast = (message, kind) => window.ofToast?.(message, kind ? { kind } : {});

  const clock = (seconds) => {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = String(total % 60).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
  };
  /* "1:30", "90", "1m30s" or "1h2m3s" to seconds; null when it is not a time. */
  function parseTime(text) {
    const value = String(text || '').trim().toLowerCase();
    if (!value) return null;
    let match = value.match(/^(?:(\d{1,3}):)?(\d{1,4}):(\d{1,2})$/);
    if (match) {
      const [hours, minutes, seconds] = [match[1], match[2], match[3]].map((part) => Number(part || 0));
      if (seconds >= 60 || (match[1] !== undefined && minutes >= 60)) return null;
      return hours * 3600 + minutes * 60 + seconds;
    }
    match = value.match(/^(?:(\d{1,5})h)?(?:(\d{1,6})m)?(?:(\d{1,7})s?)?$/);
    if (!match || !(match[1] || match[2] || match[3])) return null;
    return Number(match[1] || 0) * 3600 + Number(match[2] || 0) * 60 + Number(match[3] || 0);
  }

  async function savePlayback(patch) {
    const response = await fetch('/api/playback/preferences', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `HTTP ${response.status}`);
    }
    return (await response.json()).preferences || {};
  }

  /* ---- the player's clock: what Share and Autoplay both read ---- */
  const layout = document.querySelector('[data-watch-layout]');
  const prefs = document.querySelector('[data-watch-prefs]');
  const playerClock = { seconds: Number(layout?.dataset.resume || 0), duration: 0, paused: true, heard: false };

  /* ---- Autoplay ---- */
  const isShort = Boolean(document.querySelector('.watch-player-shell.is-short'));
  let autoplayOn = prefs?.dataset.autoplay === '1';

  function listHasNext() {
    return Boolean(document.querySelector('[data-watch-list] [data-list-next] a[href^="/watch/"]'));
  }
  function durationOf(row) {
    const text = row.querySelector('.duration')?.textContent || '';
    const parts = text.trim().split(':').map(Number);
    if (!parts.length || parts.some((part) => !Number.isFinite(part))) return 0;
    return parts.reduce((total, part) => total * 60 + part, 0);
  }
  /* The first Up next video the list shows (the This channel filter hides the rest), never
     a Short: Up next leaves them out already, and this checks again, as db.is_short_video does. */
  function nextVideo() {
    const rows = [...document.querySelectorAll('#tab-suggestions [data-video-card]')].filter((row) => !row.hidden);
    for (const row of rows) {
      const link = row.querySelector('a[href^="/watch/"]');
      if (!link || /\/shorts\b/.test(link.getAttribute('href') || '')) continue;
      const seconds = durationOf(row);
      if (seconds > 0 && seconds <= 90) continue;
      return {
        href: link.href,
        title: (row.querySelector('h2')?.textContent || link.textContent || '').trim(),
        channel: (row.querySelector('.media-row-copy p a')?.textContent || '').trim(),
        thumb: row.querySelector('.thumb img')?.getAttribute('src') || '',
        duration: row.querySelector('.duration')?.textContent?.trim() || '',
      };
    }
    return null;
  }
  function sendAutoplayState() {
    if (!layout || !prefs || isShort) return;
    tell({ type: 'openfeed:autoplay-state', on: autoplayOn });
  }

  const COUNTDOWN_SECONDS = 8;
  let card = null;
  let countdownTimer = 0;
  let cancelledAtEnd = false;
  let leaving = false;
  let played = false;

  function hideCard(restoreFocus) {
    window.clearInterval(countdownTimer);
    countdownTimer = 0;
    if (!card) return;
    const hadFocus = card.contains(document.activeElement);
    card.remove();
    card = null;
    if (hadFocus && restoreFocus) frame()?.focus();
  }
  function go(href) {
    if (leaving) return;
    leaving = true;
    hideCard(false);
    window.ofNavPending?.();
    window.location.href = href;
  }
  function showCard(next) {
    const shell = document.querySelector('.watch-player-shell');
    if (!shell || card) return;
    card = document.createElement('div');
    card.className = 'autoplay-card';
    card.dataset.autoplayCard = '';
    card.setAttribute('role', 'group');
    card.setAttribute('aria-label', 'Up next');
    const panel = document.createElement('div');
    panel.className = 'autoplay-panel';
    const eyebrow = document.createElement('p');
    eyebrow.className = 'autoplay-eyebrow';
    const count = document.createElement('span');
    count.dataset.autoplayCount = '';
    count.textContent = String(COUNTDOWN_SECONDS);
    eyebrow.append('Up next in ', count);
    const meter = document.createElement('span');
    meter.className = 'autoplay-meter';
    meter.setAttribute('aria-hidden', 'true');
    meter.style.setProperty('--autoplay-seconds', `${COUNTDOWN_SECONDS}s`);
    const link = document.createElement('a');
    link.className = 'autoplay-next';
    link.href = next.href;
    const thumb = document.createElement('span');
    thumb.className = 'autoplay-thumb';
    if (next.thumb) {
      const image = document.createElement('img');
      image.src = next.thumb;
      image.alt = '';
      image.addEventListener('error', () => image.remove());
      thumb.append(image);
    }
    if (next.duration) {
      const length = document.createElement('span');
      length.className = 'duration';
      length.textContent = next.duration;
      thumb.append(length);
    }
    const copy = document.createElement('span');
    copy.className = 'autoplay-copy';
    const title = document.createElement('strong');
    title.textContent = next.title || 'Next video';
    const channel = document.createElement('small');
    channel.textContent = next.channel;
    copy.append(title, channel);
    link.append(thumb, copy);
    const actions = document.createElement('div');
    actions.className = 'autoplay-actions';
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'btn';
    cancel.dataset.autoplayCancel = '';
    cancel.textContent = 'Cancel';
    const now = document.createElement('button');
    now.type = 'button';
    now.className = 'btn btn-primary';
    now.dataset.autoplayPlay = '';
    now.textContent = 'Play now';
    actions.append(cancel, now);
    /* Said once, not every second. */
    const spoken = document.createElement('p');
    spoken.className = 'sr-only';
    spoken.setAttribute('role', 'status');
    panel.append(eyebrow, meter, link, actions, spoken);
    card.append(panel);
    shell.append(card);
    window.setTimeout(() => { spoken.textContent = `Up next in ${COUNTDOWN_SECONDS} seconds: ${title.textContent}. Cancel to stay on this video.`; }, 50);

    cancel.addEventListener('click', () => { cancelledAtEnd = true; hideCard(true); });
    now.addEventListener('click', () => go(next.href));
    link.addEventListener('click', (event) => { event.preventDefault(); go(next.href); });
    let left = COUNTDOWN_SECONDS;
    countdownTimer = window.setInterval(() => {
      left -= 1;
      count.textContent = String(Math.max(0, left));
      if (left <= 0) go(next.href);
    }, 1000);
  }
  function onClock(data) {
    playerClock.seconds = Number(data.seconds) || 0;
    playerClock.duration = Number(data.duration) || 0;
    playerClock.paused = Boolean(data.paused);
    playerClock.heard = true;
    if (!layout || !prefs || isShort) return;
    if (!data.paused) {
      played = true;
      cancelledAtEnd = false;
      hideCard(false);
      return;
    }
    const duration = playerClock.duration;
    const atEnd = duration > 0 && playerClock.seconds >= duration - 0.75;
    if (!atEnd) { hideCard(false); return; }
    /* Only after it has actually played, so a resume point at the very end does not skip
       the video before it starts. A list's own next item comes first. */
    if (!played || !autoplayOn || cancelledAtEnd || leaving || listHasNext()) return;
    const next = nextVideo();
    if (next) showCard(next);
  }
  function setAutoplay(on, from) {
    autoplayOn = Boolean(on);
    if (!autoplayOn) hideCard(false);
    if (from !== 'player') sendAutoplayState();
    document.querySelectorAll('[data-playback-pref="autoplay"]').forEach((input) => { input.checked = autoplayOn; });
  }

  window.addEventListener('message', async (event) => {
    if (!fromPlayer(event)) return;
    const data = event.data || {};
    if (data.type === 'openfeed:clock') onClock(data);
    else if (data.type === 'openfeed:shell-ready' || data.type === 'openfeed:playback-ready') sendAutoplayState();
    else if (data.type === 'openfeed:autoplay-set') {
      const on = Boolean(data.on);
      const before = autoplayOn;
      setAutoplay(on, 'player');
      try {
        await savePlayback({ autoplay: on });
        toast(on ? 'Autoplay is on' : 'Autoplay is off');
      } catch (error) {
        setAutoplay(before);
        toast(`Could not save Autoplay: ${error.message}`, 'error');
      }
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !card || document.querySelector('dialog[open]')) return;
    event.preventDefault();
    cancelledAtEnd = true;
    hideCard(true);
  });
  sendAutoplayState();

  /* ---- Share ---- */
  const share = document.querySelector('[data-share-dialog]');
  if (share) setupShare(share);

  function setupShare(dialog) {
    const videoId = dialog.dataset.videoId || '';
    const field = dialog.querySelector('[data-share-url]');
    const copy = dialog.querySelector('[data-share-copy]');
    const startOn = dialog.querySelector('[data-share-start-on]');
    const startTime = dialog.querySelector('[data-share-start-time]');
    const error = dialog.querySelector('[data-share-error]');
    let opener = null;

    const target = () => dialog.querySelector('input[name="share_target"]:checked')?.value || 'openfeed';
    function link() {
      const seconds = startOn.checked ? parseTime(startTime.value) : null;
      if (target() === 'youtube') {
        const url = new URL('https://www.youtube.com/watch');
        url.searchParams.set('v', videoId);
        if (seconds) url.searchParams.set('t', `${seconds}s`);
        return url.toString();
      }
      const url = new URL(`/watch/${encodeURIComponent(videoId)}`, window.location.origin);
      if (seconds) url.searchParams.set('t', String(seconds));
      return url.toString();
    }
    function refresh() {
      const bad = startOn.checked && parseTime(startTime.value) === null;
      error.textContent = bad ? 'Write the time as minutes and seconds, such as 1:30.' : '';
      error.hidden = !bad;
      startTime.setAttribute('aria-invalid', bad ? 'true' : 'false');
      startTime.disabled = !startOn.checked;
      copy.disabled = bad;
      field.value = link();
    }
    function open(trigger) {
      opener = trigger;
      document.querySelectorAll('.yt-menu[open]').forEach((menu) => menu.removeAttribute('open'));
      startTime.value = clock(playerClock.seconds);
      startOn.checked = false;
      refresh();
      dialog.showModal();
      field.focus();
      field.select();
    }
    document.addEventListener('click', (event) => {
      const trigger = event.target.closest('[data-share-open]');
      if (!trigger) return;
      event.preventDefault();
      open(trigger);
    });
    dialog.addEventListener('close', () => { opener?.focus({ preventScroll: true }); opener = null; });
    dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
    dialog.addEventListener('change', refresh);
    startTime.addEventListener('input', refresh);
    startTime.addEventListener('blur', () => {
      const seconds = parseTime(startTime.value);
      if (seconds !== null) { startTime.value = clock(seconds); refresh(); }
    });
    field.addEventListener('focus', () => field.select());
    copy.addEventListener('click', async () => {
      refresh();
      if (copy.disabled) return;
      try {
        await navigator.clipboard.writeText(field.value);
      } catch (failure) {
        /* No clipboard on plain http: select it, and the system copy does the rest. */
        field.focus();
        field.select();
        try { document.execCommand('copy'); } catch (ignored) { /* selected for a manual copy */ }
      }
      toast('Link copied');
      copy.textContent = 'Copied';
      window.setTimeout(() => { copy.textContent = 'Copy'; }, 1600);
    });
  }

  /* ---- Settings, Playback: Player ---- */
  document.addEventListener('change', async (event) => {
    const control = event.target.closest('[data-playback-pref]');
    if (!control) return;
    const key = control.dataset.playbackPref;
    const label = control.dataset.prefLabel || 'that';
    const isSwitch = control.type === 'checkbox';
    let value = isSwitch ? control.checked : control.value;
    if (key === 'default_speed') value = Number(value) || 1;
    const patch = { [key]: value };
    /* A language picked here is a manual choice: creator captions first. */
    if (key === 'captions_language') patch.captions_auto = false;
    const previous = isSwitch ? !control.checked
      : control.dataset.previous ?? [...control.options].find((option) => option.defaultSelected)?.value ?? '';
    const row = control.closest('.toggle-row, .set-row') || control.parentElement;
    let state = row.querySelector('.set-state');
    if (!state) {
      state = document.createElement('span');
      state.className = 'set-state';
      state.setAttribute('role', 'status');
      control.before(state);
    }
    window.clearTimeout(state.clearTimer);
    state.textContent = 'Saving';
    state.dataset.kind = '';
    control.disabled = true;
    try {
      await savePlayback(patch);
      state.textContent = 'Saved';
      state.dataset.kind = 'saved';
      state.clearTimer = window.setTimeout(() => state.remove(), 2000);
      if (!isSwitch) control.dataset.previous = control.value;
      if (key === 'autoplay') setAutoplay(value);
      const shown = isSwitch ? (value ? 'on' : 'off') : control.selectedOptions?.[0]?.textContent || value;
      toast(`${label}: ${shown}`, 'success');
    } catch (error) {
      if (isSwitch) control.checked = previous; else control.value = previous;
      state.textContent = 'Not saved';
      state.dataset.kind = 'failed';
      toast(`Could not save ${label}: ${error.message}`, 'error');
    } finally {
      control.disabled = false;
    }
  });
  /* A select's value before this change, so a failed save can put it back. */
  document.addEventListener('focusin', (event) => {
    const select = event.target.closest('select[data-playback-pref]');
    if (select) select.dataset.previous = select.value;
  });
})();
