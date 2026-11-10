/* Watch page side of the player's media controls, and the chat panel.

   - The c key toggles captions while focus is on the page rather than in the player.
   - Media Session next/previous (lock screen, headphones) come up from the player as
     messages: next opens the first visible Up next video, previous goes back when the
     last page was another video.
   - The chat panel shows live chat, or chat replay in step with the player clock. All of
     it comes from /api/livechat on this server; the browser never talks to YouTube.
   - The Live chat switch in Settings, Integrations. */
(() => {
  'use strict';

  const frame = () => document.querySelector('.player-frame');
  const tell = (message) => {
    const player = frame();
    if (player && player.contentWindow) player.contentWindow.postMessage(message, window.location.origin);
  };
  const typing = (target) => {
    const tag = String((target && target.tagName) || '').toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || Boolean(target && target.isContentEditable);
  };

  /* ---- captions key and the up-next queue ---- */
  function nextRow() {
    /* Playing through a list (?list=): the next list item comes before any suggestion. */
    const listed = document.querySelector('[data-watch-list] [data-list-next]');
    if (listed) return listed;
    return [...document.querySelectorAll('#tab-suggestions [data-video-card]')].find((row) => !row.hidden && row.querySelector('a[href^="/watch/"]'));
  }
  function cameFromVideo() {
    try {
      const previous = new URL(document.referrer);
      return previous.origin === window.location.origin && previous.pathname.startsWith('/watch/') && previous.pathname !== window.location.pathname;
    } catch (error) { return false; }
  }
  function sendQueue() {
    if (!document.querySelector('[data-watch-layout]')) return;
    tell({ type: 'openfeed:media-queue', next: Boolean(nextRow()), previous: cameFromVideo() && window.history.length > 1 });
  }

  window.addEventListener('message', (event) => {
    if (event.origin !== window.location.origin || event.source !== frame()?.contentWindow) return;
    const type = (event.data || {}).type;
    if (type === 'openfeed:shell-ready' || type === 'openfeed:playback-ready') sendQueue();
    else if (type === 'openfeed:media-next') {
      const link = nextRow()?.querySelector('a[href^="/watch/"]');
      if (link) window.location.href = link.href;
    } else if (type === 'openfeed:media-previous') {
      if (cameFromVideo()) window.history.back();
    } else if (type === 'openfeed:theater-toggle') {
      /* t pressed inside the player, where the page's own t listener cannot hear it. */
      document.querySelector('[data-theater-toggle]')?.click();
    }
  });
  document.addEventListener('click', (event) => {
    if (event.target.closest('[data-upnext-filter]')) window.setTimeout(sendQueue, 0);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'c' && event.key !== 'C') return;
    if (event.metaKey || event.ctrlKey || event.altKey || typing(event.target)) return;
    if (!frame() || !document.querySelector('[data-watch-layout]')) return;
    if (event.target.closest('dialog[open], .yt-menu[open]')) return;
    event.preventDefault();
    tell({ type: 'openfeed:captions-toggle' });
  });

  /* ---- player shortcuts with focus on the page ----
     Clicking the scrubber leaves focus here (video.js cancels the mousedown), so the
     player's keys have to work from the page too. Letters work anywhere but a field;
     Space, the arrows and the number row only while nothing else has focus, since they
     already mean something to a focused button, link or list. The player does the rest
     and answers t and N itself the same way, so both work wherever focus is. */
  const LETTER_KEYS = new Set(['k', 'K', 'j', 'J', 'l', 'L', 'm', 'M', '<', '>', ',', '.']);
  const BARE_KEYS = new Set([' ', 'ArrowLeft', 'ArrowRight', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9']);
  document.addEventListener('keydown', (event) => {
    if (event.metaKey || event.ctrlKey || event.altKey || event.defaultPrevented || typing(event.target)) return;
    if (!frame() || !document.querySelector('[data-watch-layout]')) return;
    if (document.querySelector('dialog[open], .yt-menu[open]')) return;
    const key = event.key;
    const bare = event.target === document.body || event.target === document.documentElement;
    if (key === 'N') {
      const link = nextRow()?.querySelector('a[href^="/watch/"]');
      if (!link) return;
      event.preventDefault();
      window.location.href = link.href;
    } else if (key === 'P') {
      if (!cameFromVideo()) return;
      event.preventDefault();
      window.history.back();
    } else if (key === 'f' || key === 'F') {
      /* Fullscreen needs this page's own gesture, so the frame goes fullscreen from here. */
      event.preventDefault();
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
      else frame().requestFullscreen?.().catch(() => {});
    } else if (LETTER_KEYS.has(key) || (bare && BARE_KEYS.has(key) && !String(event.code || '').startsWith('Numpad'))) {
      event.preventDefault();
      tell({ type: 'openfeed:key', key });
    }
  });
  sendQueue();

  /* ---- chat panel ---- */
  const panel = document.querySelector('[data-chat-panel]');
  if (panel) setupChat(panel);

  function setupChat(card) {
    const videoId = card.dataset.videoId || '';
    const list = card.querySelector('[data-chat-list]');
    const title = card.querySelector('[data-chat-title]');
    const state = card.querySelector('[data-chat-state]');
    const note = card.querySelector('[data-chat-note]');
    const base = `/api/livechat/${encodeURIComponent(videoId)}`;
    const MAX_ROWS = 150;
    const hue = (name) => [...String(name)].reduce((sum, char) => sum + char.codePointAt(0), 0) % 360;
    const initials = (name) => {
      const words = String(name || '').replace(/^@/, '').split(/[\s_.-]+/).filter(Boolean);
      if (!words.length) return '?';
      return (words.length === 1 ? words[0].slice(0, 2) : words[0][0] + words[1][0]).toUpperCase();
    };
    const clock = (seconds) => {
      const total = Math.max(0, Math.floor(Number(seconds) || 0));
      const h = Math.floor(total / 3600);
      const m = Math.floor((total % 3600) / 60);
      const s = String(total % 60).padStart(2, '0');
      return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
    };
    const setState = (text) => { state.textContent = text; };
    const setNote = (text) => { note.textContent = text; note.hidden = !text; };

    /* Follow the bottom, unless the reader has scrolled up to read something. */
    let pinned = true;
    list.addEventListener('scroll', () => {
      pinned = list.scrollHeight - list.scrollTop - list.clientHeight < 24;
    }, { passive: true });
    function row(message) {
      const item = document.createElement('li');
      item.className = `chat-msg chat-${message.kind || 'text'}`;
      if (message.role) item.dataset.role = message.role;
      const avatar = document.createElement('span');
      avatar.className = 'avatar';
      avatar.style.setProperty('--avatar-hue', String(hue(message.author || '')));
      avatar.setAttribute('aria-hidden', 'true');
      avatar.textContent = initials(message.author);
      const body = document.createElement('p');
      const author = document.createElement('strong');
      author.className = 'chat-author';
      author.textContent = message.author || 'Someone';
      body.appendChild(author);
      if (message.role === 'owner' || message.role === 'moderator' || message.role === 'member') {
        const badge = document.createElement('span');
        badge.className = 'chat-role';
        badge.textContent = message.role === 'owner' ? 'Creator' : message.role === 'moderator' ? 'Mod' : 'Member';
        body.appendChild(badge);
      }
      if (message.amount) {
        const amount = document.createElement('span');
        amount.className = 'chat-amount';
        amount.textContent = message.amount;
        body.appendChild(amount);
      }
      const text = document.createElement('span');
      text.className = 'chat-text';
      text.textContent = message.text || '';
      body.appendChild(text);
      item.append(avatar, body);
      return item;
    }
    function append(messages) {
      if (!messages.length) return;
      list.append(...messages.map(row));
      while (list.children.length > MAX_ROWS) list.firstElementChild.remove();
      if (pinned) list.scrollTop = list.scrollHeight;
    }
    function show(mode) {
      card.hidden = false;
      card.dataset.mode = mode;
      title.textContent = mode === 'replay' ? 'Chat replay' : 'Live chat';
    }
    async function getJSON(url) {
      const response = await fetch(url, { headers: { Accept: 'application/json' } });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      return data;
    }

    /* Live: poll while the page is visible. The server's follower stops on its own soon
       after the polls do, so a closed tab costs nothing for long. */
    function runLive() {
      show('live');
      setState('Connecting');
      let after = 0;
      let timer = 0;
      let failures = 0;
      async function poll() {
        window.clearTimeout(timer);
        if (document.visibilityState === 'hidden') return;
        try {
          const data = await getJSON(`${base}/live?after=${after}`);
          failures = 0;
          after = Math.max(after, Number(data.seq) || 0);
          append(data.messages || []);
          if (data.state === 'busy' || data.state === 'error') {
            setState(data.state === 'busy' ? 'Busy' : 'Unavailable');
            setNote(data.error || 'Chat could not be read.');
            timer = window.setTimeout(poll, 30000);
            return;
          }
          if (data.state === 'ended') {
            setState('Ended');
            setNote('The stream has ended. Reopen the video later for the replay.');
            return;
          }
          setState(data.state === 'live' ? 'Live' : 'Connecting');
          setNote('');
        } catch (error) {
          failures += 1;
          setState('Unavailable');
          setNote(`Chat could not be read: ${error.message}.`);
        }
        timer = window.setTimeout(poll, failures ? Math.min(30000, 3000 * 2 ** failures) : 3000);
      }
      document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') poll(); });
      poll();
    }

    /* Replay: messages carry their offset into the video, and the player reports its clock,
       so the list shows what had been said by the moment on screen. */
    function runReplay() {
      show('replay');
      setState('Loading');
      setNote('');
      const WINDOW = 180;
      const EMPTY = 'No chat yet at this point in the video.';
      let messages = [];
      let from = 0;
      let to = -1;
      let loading = false;
      let lastLoad = 0;
      let shownUpTo = -1;
      let now = Number(document.querySelector('[data-watch-layout]')?.dataset.resume || 0);
      let complete = false;
      let failed = false;
      let again = 0;

      async function load(at) {
        loading = true;
        lastLoad = Date.now();
        const start = Math.max(0, Math.floor(at) - 20);
        try {
          const data = await getJSON(`${base}/replay?start=${start}&end=${start + WINDOW}`);
          if (data.state === 'error') throw new Error(data.error || 'unknown error');
          const through = Number(data.through) || 0;
          complete = data.state === 'ready';
          messages = (data.messages || []).slice().sort((a, b) => a.t - b.t);
          from = start;
          /* While the download is still running, only trust the stretch it has reached. */
          to = complete ? start + WINDOW : Math.min(start + WINDOW, through);
          failed = false;
          if (!complete) setState(`Loading, ${clock(through)} read`);
          else setState(data.partial ? 'Partial' : '');
          if (complete && !data.count) setNote('This replay has no messages.');
          else if (complete && data.partial) setNote(`Only the first ${clock(through)} of chat could be read.`);
          else if (complete && data.truncated) setNote('This replay is very long, so chat after the first part is not shown.');
          else setNote('');
        } catch (error) {
          failed = true;
          setState('Unavailable');
          setNote(`Chat replay could not be read: ${error.message}.`);
        }
        loading = false;
        paint(true);
        /* Still downloading: look again even if the player is paused and sends no clock. */
        window.clearTimeout(again);
        if (!complete && !failed) again = window.setTimeout(() => { if (!loading) load(now); }, 4000);
      }
      function paint(reset) {
        const ms = now * 1000;
        if (reset || ms < shownUpTo) {
          list.replaceChildren();
          shownUpTo = -1;
          pinned = true;
        }
        const fresh = messages.filter((message) => message.t > shownUpTo && message.t <= ms);
        if (fresh.length) {
          append(fresh.slice(-MAX_ROWS));
          shownUpTo = fresh[fresh.length - 1].t;
        }
        if (failed) return;
        if (!list.children.length && complete && messages.length) setNote(EMPTY);
        else if (note.textContent === EMPTY && list.children.length) setNote('');
      }
      function tick(seconds) {
        const jumped = Math.abs(seconds - now) > 4;
        now = seconds;
        const outside = seconds < from || seconds > to - 15;
        if (outside && !loading && (jumped || Date.now() - lastLoad > 4000)) { load(seconds); return; }
        paint(jumped);
      }
      window.addEventListener('message', (event) => {
        if (event.origin !== window.location.origin || event.source !== frame()?.contentWindow) return;
        const data = event.data || {};
        if (data.type === 'openfeed:clock') tick(Number(data.seconds) || 0);
      });
      load(now);
    }

    getJSON(base)
      .then((data) => {
        if (data.mode === 'live') runLive();
        else if (data.mode === 'replay') runReplay();
        else if (data.mode === 'upcoming') {
          show('live');
          setState('Not started');
          setNote(data.reason || 'Chat can be read once the stream is live.');
        }
        /* A video with no chat at all keeps the panel out of the way. */
      })
      .catch(() => {});
  }

  /* ---- Settings: the Live chat switch ---- */
  document.addEventListener('change', async (event) => {
    const input = event.target.closest('[data-setting-live-chat]');
    if (!input) return;
    const on = input.checked;
    const row = input.closest('.toggle-row') || input.parentElement;
    let status = row.querySelector(':scope .set-state');
    if (!status) {
      status = document.createElement('span');
      status.className = 'set-state';
      status.setAttribute('role', 'status');
      input.before(status);
    }
    status.textContent = 'Saving';
    status.dataset.kind = '';
    input.disabled = true;
    try {
      const response = await fetch('/api/playback/preferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ live_chat_enabled: on }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      status.textContent = 'Saved';
      status.dataset.kind = 'saved';
      window.setTimeout(() => status.remove(), 2000);
      window.ofToast?.(on ? 'Live chat on. It appears on live streams and past streams with a replay.' : 'Live chat off', { kind: 'success' });
    } catch (error) {
      input.checked = !on;
      status.textContent = 'Not saved';
      status.dataset.kind = 'failed';
      window.ofToast?.(`Could not save Live chat: ${error.message}`, { kind: 'error' });
    } finally {
      input.disabled = false;
    }
  });
})();
