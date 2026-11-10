/* Discovery helpers shared by every page: DeArrow titles and thumbnails, and
   "Don't recommend this channel" from a card's menu. Loaded into the shell bundle. */
(() => {
  /* ---- DeArrow ---------------------------------------------------------------
     The server renders the creator's title. When the user has DeArrow on, the body
     carries data-dearrow, and this asks the server (never DeArrow) for community titles
     and for frames this server has cut, then swaps them in. The original stays on the
     element and one press away: a menu row on cards, a button on the watch page. */
  const body = document.body;
  const DEARROW_ON = body?.dataset.dearrow === 'on';
  const LOWERCASE = body?.dataset.dearrowCasing === 'lowercase';
  const BATCH = 100;

  /* DeArrow's lowercase format (toLowerCaseTitle): every word lowercased, except a word
     containing Greek letters in a title that is not Greek; Turkish rules for text with a
     dotted or dotless I; and the ">" keep-as-written marker removed. The server's
     dearrow.to_lowercase is the same rules and is what the tests pin down. */
  const GREEK = /[\u0370-\u03ff\u1f00-\u1fff]/;
  function dearrowLowercase(title) {
    const text = String(title || '');
    const letters = (text.match(/\p{L}/gu) || []).length || 1;
    const greek = (text.match(/[\u0370-\u03ff\u1f00-\u1fff]/g) || []).length;
    const isGreek = greek / letters > 0.5;
    const turkic = /[ıİ]/.test(text);
    const words = text.split(' ').map((word) => {
      if (!isGreek && GREEK.test(word)) return word;
      return (turkic ? word.replace(/I/g, 'ı').replace(/İ/g, 'i') : word).toLowerCase();
    });
    return words.join(' ').replace(/(^|\s)>(\S)/g, '$1$2').trim();
  }

  function targetsIn(root) {
    const found = [];
    root.querySelectorAll?.('[data-video-card][data-video-id]:not([data-dearrow-seen]), [data-dearrow-video]:not([data-dearrow-seen])').forEach((node) => {
      const id = node.dataset.videoId || node.dataset.dearrowVideo;
      const title = titleOf(node);
      if (id && title) found.push({ node, id, title });
    });
    const watch = document.querySelector('[data-watch-layout][data-video-id]:not([data-dearrow-seen])');
    if (watch && (root === document || root.contains?.(watch))) {
      const title = watch.querySelector('.watch-title');
      if (title) found.push({ node: watch, id: watch.dataset.videoId, title, watch: true });
    }
    return found;
  }

  /* The watch layout contains cards of its own (related videos), so everything is looked
     up through its owner: a card's menu row belongs to that card, never to the page. */
  function ownerOf(element) {
    return element.closest('[data-video-card], [data-dearrow-video], [data-watch-layout]');
  }

  function titleOf(node) {
    return node.matches('[data-watch-layout]') ? node.querySelector('.watch-title') : node.querySelector('h3 a, h2 a');
  }

  function imageOf(node) {
    return node.matches('[data-watch-layout]') ? null : node.querySelector('.thumb img, .short-thumb img');
  }

  function togglesOf(node) {
    return [...node.querySelectorAll('[data-dearrow-toggle]')].filter((button) => ownerOf(button) === node);
  }

  function show(target, which) {
    const { node, title } = target;
    const useCommunity = which === 'dearrow';
    const text = useCommunity ? title.dataset.dearrowTitle : title.dataset.originalShown;
    if (text) title.textContent = text;
    title.classList.toggle('is-dearrow', useCommunity && Boolean(title.dataset.dearrowTitle));
    title.title = useCommunity && title.dataset.dearrowTitle ? `Original title: ${title.dataset.originalTitle}` : '';
    const img = imageOf(node);
    if (img && img.dataset.dearrowSrc) img.src = useCommunity ? img.dataset.dearrowSrc : img.dataset.originalSrc;
    node.dataset.dearrowShowing = which;
    togglesOf(node).forEach((button) => {
      const label = useCommunity ? 'Show original title' : 'Show DeArrow title';
      const span = button.querySelector('.menu-label');
      if (span) span.textContent = label; else button.textContent = label;
      button.setAttribute('aria-pressed', useCommunity ? 'false' : 'true');
    });
  }

  function addToggle(target) {
    const { node } = target;
    if (togglesOf(node).length) return;
    if (target.watch) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'dearrow-toggle';
      button.setAttribute('data-dearrow-toggle', '');
      target.title.after(button);
      return;
    }
    const panel = node.querySelector('.menu-panel');
    if (!panel) return;
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'menu-row';
    row.setAttribute('role', 'menuitem');
    row.setAttribute('data-dearrow-toggle', '');
    row.innerHTML = '<span class="menu-icon"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h13"/><path d="m11 6 6 6-6 6"/><path d="M21 5v14"/></svg></span><span class="menu-copy"><span class="menu-label"></span></span><span class="menu-trailing"></span>';
    panel.prepend(row);
  }

  function apply(target, item) {
    const { title, node } = target;
    const original = title.textContent.trim();
    title.dataset.originalTitle = original;
    title.dataset.originalShown = LOWERCASE ? dearrowLowercase(original) : original;
    let changed = false;
    if (item?.title && item.title !== original) {
      title.dataset.dearrowTitle = item.title;
      changed = true;
    }
    const img = imageOf(node);
    if (img && item?.thumbnail) {
      img.dataset.originalSrc = img.getAttribute('src') || '';
      img.dataset.dearrowSrc = item.thumbnail;
      img.addEventListener('error', () => {
        if (img.src.includes('/api/dearrow/')) { delete img.dataset.dearrowSrc; img.src = img.dataset.originalSrc; }
      });
      changed = true;
    }
    if (changed) {
      addToggle(target);
      show(target, 'dearrow');
    } else if (LOWERCASE && title.dataset.originalShown !== original) {
      title.textContent = title.dataset.originalShown;
    }
  }

  async function processRoot(root) {
    const targets = targetsIn(root);
    if (!targets.length) return;
    targets.forEach((target) => target.node.setAttribute('data-dearrow-seen', ''));
    const ids = [...new Set(targets.map((target) => target.id))];
    for (let start = 0; start < ids.length; start += BATCH) {
      const chunk = ids.slice(start, start + BATCH);
      let items = {};
      try {
        const response = await fetch(`/api/dearrow/branding?ids=${encodeURIComponent(chunk.join(','))}`);
        if (response.ok) items = (await response.json()).items || {};
      } catch (error) {
        items = {};
      }
      targets.filter((target) => chunk.includes(target.id)).forEach((target) => apply(target, items[target.id]));
    }
  }

  if (DEARROW_ON) {
    let pending = false;
    const schedule = () => {
      if (pending) return;
      pending = true;
      window.setTimeout(() => { pending = false; processRoot(document); }, 120);
    };
    processRoot(document);
    /* Infinite scroll, channel tabs and search pages append cards after load. */
    new MutationObserver((records) => {
      if (records.some((record) => record.addedNodes.length)) schedule();
    }).observe(document.querySelector('main') || document.body, { childList: true, subtree: true });

    document.addEventListener('click', (event) => {
      const button = event.target.closest('[data-dearrow-toggle]');
      if (!button) return;
      event.preventDefault();
      const node = ownerOf(button);
      const title = node && titleOf(node);
      if (!node || !title || !node.dataset.dearrowShowing) return;
      show({ node, title }, node.dataset.dearrowShowing === 'dearrow' ? 'original' : 'dearrow');
      button.closest('.yt-menu')?.removeAttribute('open');
    });
  }

  /* ---- Don't recommend this channel ------------------------------------------
     Adds the creator to the same muted_channels list the feed controls dialog edits,
     through an endpoint that changes one entry, so it can never overwrite the rest. */
  async function setChannelMuted(key, muted) {
    const response = await fetch('/api/feed/muted-channels', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channel_key: key, muted })
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  function cardsForChannel(channelId) {
    const wanted = String(channelId).toLowerCase();
    return [...document.querySelectorAll('[data-video-card][data-channel-id]')]
      .filter((card) => card.dataset.channelId.toLowerCase() === wanted);
  }

  /* A shelf whose every card is gone would leave a heading over nothing. */
  function syncSections() {
    document.querySelectorAll('.feed-section, .shelf-section, [data-feed-section]').forEach((section) => {
      const cards = section.querySelectorAll('[data-video-card]');
      if (!cards.length) return;
      const allHidden = [...cards].every((card) => card.hidden);
      if (allHidden) section.dataset.mutedEmpty = '';
      else delete section.dataset.mutedEmpty;
      section.hidden = allHidden;
    });
  }

  document.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-mute-from-card]');
    if (!button) return;
    event.preventDefault();
    const key = button.dataset.channelKey;
    const name = button.dataset.channelTitle || 'this channel';
    button.closest('.yt-menu')?.removeAttribute('open');
    if (!key) return;
    button.disabled = true;
    try {
      await setChannelMuted(key, true);
      const cards = cardsForChannel(key).filter((card) => !card.hidden);
      cards.forEach((card) => { card.hidden = true; });
      syncSections();
      window.ofToast(`You won't see ${name} in recommendations`, {
        kind: 'success',
        actionLabel: 'Undo',
        action: async () => {
          try {
            await setChannelMuted(key, false);
            cards.forEach((card) => { card.hidden = false; });
            syncSections();
            window.ofToast(`${name} can be recommended again`);
          } catch (error) {
            window.ofToast('Could not undo. Unmute the creator in Feed controls.', { kind: 'error' });
          }
        }
      });
    } catch (error) {
      window.ofToast(`Could not mute ${name}. Nothing was changed.`, { kind: 'error' });
    } finally {
      button.disabled = false;
    }
  });

  window.ofDeArrowLowercase = dearrowLowercase;
})();
