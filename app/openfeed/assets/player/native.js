/* Control bar for the players that are a plain <video>: the downloaded file and the yt-dlp
   stream. These pages must keep working when Invidious is down, so they cannot borrow the
   video.js that lives behind it. The markup reuses video.js class names so chrome.css
   draws both the same way. Native controls stay on until this script has run. */
(() => {
  const root = document.querySelector('[data-native-player]');
  const video = root && root.querySelector('video');
  if (!video) return;
  const $ = (selector) => root.querySelector(selector);
  const clock = (seconds) => {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = String(total % 60).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
  };
  video.controls = false;
  root.classList.add('vjs-enhanced');

  const play = $('.vjs-play-control');
  const big = $('.vjs-big-play-button');
  const mute = $('.vjs-mute-control');
  const volume = $('.vjs-volume-bar');
  const level = $('.vjs-volume-level');
  const holder = $('.vjs-progress-holder');
  const loaded = $('.vjs-load-progress');
  const played = $('.vjs-play-progress');
  const mouse = $('.vjs-mouse-display');
  const tip = mouse.querySelector('.vjs-time-tooltip');
  const current = $('.vjs-current-time-display');
  const total = $('.vjs-duration-display');
  const full = $('.vjs-fullscreen-control');
  const pip = $('.of-pip-control');
  const rate = $('.vjs-playback-rate');
  const rateButton = rate.querySelector('button');
  const rateValue = rate.querySelector('.vjs-playback-rate-value');
  const rateItems = [...rate.querySelectorAll('li')];

  const toggle = () => (video.paused || video.ended ? video.play().catch(() => {}) : video.pause());
  function paint() {
    const paused = video.paused;
    const ended = video.ended;
    root.classList.toggle('vjs-paused', paused);
    root.classList.toggle('vjs-playing', !paused);
    root.classList.toggle('vjs-ended', ended);
    play.classList.toggle('vjs-playing', !paused);
    play.classList.toggle('vjs-ended', ended);
    const label = ended ? 'Replay' : paused ? 'Play' : 'Pause';
    play.dataset.tip = label;
    play.setAttribute('aria-label', label);
    if (!paused) root.classList.add('vjs-has-started');
    if (paused) wake();
  }
  function paintTime() {
    const duration = Number(video.duration) || 0;
    current.textContent = clock(video.currentTime);
    if (duration) total.textContent = clock(duration);
    const percent = duration ? (video.currentTime / duration) * 100 : 0;
    played.style.width = `${percent}%`;
    holder.setAttribute('aria-valuenow', percent.toFixed(2));
    holder.setAttribute('aria-valuetext', `${clock(video.currentTime)} of ${clock(duration)}`);
    if (duration && video.buffered.length) {
      loaded.style.width = `${(video.buffered.end(video.buffered.length - 1) / duration) * 100}%`;
    }
  }
  function paintVolume() {
    const value = video.muted ? 0 : video.volume;
    level.style.width = `${value * 100}%`;
    volume.setAttribute('aria-valuenow', String(Math.round(value * 100)));
    mute.classList.remove('vjs-vol-0', 'vjs-vol-1', 'vjs-vol-2', 'vjs-vol-3');
    mute.classList.add(`vjs-vol-${value === 0 ? 0 : value < 0.5 ? 1 : 3}`);
    const label = video.muted ? 'Unmute' : 'Mute';
    mute.dataset.tip = label;
    mute.setAttribute('aria-label', label);
  }

  /* The bar fades after three still seconds of playback, as video.js does. */
  let idle = 0;
  function wake() {
    root.classList.remove('vjs-user-inactive');
    window.clearTimeout(idle);
    idle = window.setTimeout(() => { if (!video.paused) root.classList.add('vjs-user-inactive'); }, 3000);
  }
  ['pointermove', 'pointerdown', 'keydown'].forEach((name) => root.addEventListener(name, wake, { passive: true }));

  function fraction(event, element) {
    const box = element.getBoundingClientRect();
    return Math.max(0, Math.min(1, (event.clientX - box.left) / box.width));
  }
  function seekTo(event) {
    if (Number(video.duration)) video.currentTime = fraction(event, holder) * video.duration;
    paintTime();
  }
  const scrubber = holder.parentElement;
  scrubber.addEventListener('pointerdown', (event) => {
    scrubber.setPointerCapture(event.pointerId);
    seekTo(event);
    const move = (moved) => seekTo(moved);
    const stop = () => { scrubber.removeEventListener('pointermove', move); scrubber.removeEventListener('pointerup', stop); scrubber.removeEventListener('pointercancel', stop); };
    scrubber.addEventListener('pointermove', move);
    scrubber.addEventListener('pointerup', stop);
    scrubber.addEventListener('pointercancel', stop);
  });
  scrubber.addEventListener('pointermove', (event) => {
    const at = fraction(event, holder);
    mouse.style.left = `${at * 100}%`;
    tip.textContent = clock(at * (Number(video.duration) || 0));
    const half = tip.offsetWidth / 2;
    const room = holder.offsetWidth * at;
    const room2 = holder.offsetWidth - room;
    tip.style.transform = `translateX(${room < half ? half - room - half : room2 < half ? -half * 2 + room2 : -half}px)`;
  });
  holder.addEventListener('keydown', (event) => {
    const step = { ArrowRight: 5, ArrowLeft: -5, PageUp: 30, PageDown: -30 }[event.key];
    if (!step) return;
    event.preventDefault();
    event.stopPropagation();
    video.currentTime = Math.max(0, video.currentTime + step);
  });

  function setVolume(event) {
    video.muted = false;
    video.volume = fraction(event, volume);
  }
  volume.addEventListener('pointerdown', (event) => {
    volume.setPointerCapture(event.pointerId);
    setVolume(event);
    const stop = () => { volume.removeEventListener('pointermove', setVolume); volume.removeEventListener('pointerup', stop); };
    volume.addEventListener('pointermove', setVolume);
    volume.addEventListener('pointerup', stop);
  });
  volume.addEventListener('keydown', (event) => {
    const step = { ArrowUp: 0.1, ArrowRight: 0.1, ArrowDown: -0.1, ArrowLeft: -0.1 }[event.key];
    if (!step) return;
    event.preventDefault();
    event.stopPropagation();
    video.muted = false;
    video.volume = Math.max(0, Math.min(1, video.volume + step));
  });

  function setRate(value) {
    video.playbackRate = value;
    rateValue.textContent = `${value}x`;
    rateItems.forEach((item) => {
      const on = Number(item.dataset.rate) === value;
      item.classList.toggle('vjs-selected', on);
      item.setAttribute('aria-checked', on ? 'true' : 'false');
    });
  }
  function closeRate() {
    rate.classList.remove('vjs-lock-showing');
    rate.querySelector('.vjs-menu').classList.remove('vjs-lock-showing');
    rateButton.setAttribute('aria-expanded', 'false');
  }
  rateButton.addEventListener('click', (event) => {
    event.stopPropagation();
    const open = rate.querySelector('.vjs-menu').classList.toggle('vjs-lock-showing');
    rateButton.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  rateItems.forEach((item) => {
    const pick = () => { setRate(Number(item.dataset.rate)); closeRate(); rateButton.focus(); };
    item.addEventListener('click', pick);
    item.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); pick(); }
    });
  });
  document.addEventListener('click', (event) => { if (!rate.contains(event.target)) closeRate(); });

  async function toggleFull() {
    if (document.fullscreenElement) await document.exitFullscreen().catch(() => {});
    else if (root.requestFullscreen) await root.requestFullscreen().catch(() => {});
    else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();
  }
  document.addEventListener('fullscreenchange', () => {
    const on = Boolean(document.fullscreenElement);
    root.classList.toggle('vjs-fullscreen', on);
    full.dataset.tip = on ? 'Exit fullscreen' : 'Fullscreen';
    full.setAttribute('aria-label', full.dataset.tip);
  });
  if (document.pictureInPictureEnabled && !video.disablePictureInPicture) {
    pip.addEventListener('click', () => {
      (document.pictureInPictureElement ? document.exitPictureInPicture() : video.requestPictureInPicture()).catch(() => {});
    });
  } else {
    pip.hidden = true;
  }

  play.addEventListener('click', toggle);
  big.addEventListener('click', toggle);
  mute.addEventListener('click', () => { video.muted = !video.muted; });
  full.addEventListener('click', toggleFull);
  video.addEventListener('click', toggle);
  video.addEventListener('dblclick', toggleFull);

  ['play', 'pause', 'ended', 'playing'].forEach((name) => video.addEventListener(name, paint));
  ['timeupdate', 'progress', 'durationchange', 'seeked', 'loadedmetadata'].forEach((name) => video.addEventListener(name, paintTime));
  video.addEventListener('volumechange', paintVolume);
  ['waiting', 'seeking'].forEach((name) => video.addEventListener(name, () => root.classList.add('vjs-waiting')));
  ['playing', 'seeked', 'canplay'].forEach((name) => video.addEventListener(name, () => root.classList.remove('vjs-waiting')));

  root.addEventListener('keydown', (event) => {
    if (event.target.closest('.vjs-menu') || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.target.matches('button') && (event.key === ' ' || event.key === 'Enter')) return;
    const key = event.key.toLowerCase();
    if (key === ' ' || key === 'k') { event.preventDefault(); toggle(); }
    else if (key === 'arrowright' || key === 'l') { event.preventDefault(); video.currentTime += 5; }
    else if (key === 'arrowleft' || key === 'j') { event.preventDefault(); video.currentTime = Math.max(0, video.currentTime - 5); }
    else if (key === 'm') { video.muted = !video.muted; }
    else if (key === 'f') { toggleFull(); }
  });

  setRate(1);
  paint();
  paintTime();
  paintVolume();
})();
