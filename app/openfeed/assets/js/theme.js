/* ---- Appearance: Dark, Light, Use device theme ----
   appearance.py has the model. The <head> script already set data-theme before first
   paint; this keeps the controls, the browser mirror and the account copy in step.
   A change applies at once, everywhere on the page and in other open tabs, then saves. */
(() => {
  const root = document.documentElement;
  const KEY = 'openfeed-theme';
  const COOKIE = 'openfeed_theme';
  const CHOICES = ['dark', 'light', 'system'];
  const SHORT = { dark: 'Dark', light: 'Light', system: 'Device theme' };
  const COLORS = { dark: '#0a0a0b', light: '#ffffff' };

  const current = () => (CHOICES.includes(root.dataset.theme) ? root.dataset.theme : 'dark');

  /* With an explicit choice both metas carry it; device theme lets each media query pick. */
  function paintChrome(choice) {
    document.querySelectorAll('meta[name="theme-color"]').forEach((meta) => {
      const scheme = choice === 'system' ? (String(meta.media).includes('light') ? 'light' : 'dark') : choice;
      meta.content = COLORS[scheme];
    });
  }

  function remember(choice) {
    try { localStorage.setItem(KEY, choice); } catch (error) { /* private mode: the cookie still works */ }
    document.cookie = `${COOKIE}=${choice}; path=/; max-age=31536000; samesite=lax`;
  }

  function sync(choice) {
    document.querySelectorAll('[data-appearance-label]').forEach((label) => { label.textContent = SHORT[choice]; });
    document.querySelectorAll('[data-appearance-option]').forEach((option) => {
      const on = option.dataset.appearanceOption === choice;
      if (option.matches('input')) option.checked = on;
      else option.setAttribute('aria-checked', on ? 'true' : 'false');
    });
  }

  function apply(choice) {
    root.dataset.theme = choice;
    paintChrome(choice);
    remember(choice);
    sync(choice);
  }

  async function choose(choice) {
    if (!CHOICES.includes(choice)) return;
    if (choice === current()) { sync(choice); return; }
    apply(choice);
    try {
      const response = await fetch('/api/settings/appearance', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ appearance: choice })
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
    } catch (error) {
      const cause = String(error?.message || '').startsWith('HTTP ') ? `the server answered ${error.message}` : 'the server could not be reached';
      window.ofToast?.(`Appearance changed on this browser only. Saving it failed: ${cause}.`, { kind: 'error' });
    }
  }
  window.ofSetAppearance = choose;

  /* The page may come from the server with a choice this browser has not seen yet. */
  remember(current());
  paintChrome(current());
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => sync(current()));
  else sync(current());

  window.addEventListener('storage', (event) => {
    if (event.key === KEY && CHOICES.includes(event.newValue) && event.newValue !== current()) {
      root.dataset.theme = event.newValue;
      paintChrome(event.newValue);
      sync(event.newValue);
    }
  });

  /* Settings: a radio group, so arrow keys move the choice as they do in any radio group. */
  document.addEventListener('change', (event) => {
    const input = event.target.closest?.('input[data-appearance-option]');
    if (input && input.checked) choose(input.dataset.appearanceOption);
  });

  /* Account menu: "Appearance: Dark" opens a page of three options inside the same panel. */
  function showPage(panel, open) {
    if (open) panel.dataset.view = 'appearance';
    else delete panel.dataset.view;
    const target = open
      ? panel.querySelector('[data-appearance-page] [aria-checked="true"]') || panel.querySelector('[data-appearance-back]')
      : panel.querySelector('[data-appearance-open]');
    target?.focus({ preventScroll: true });
  }

  document.addEventListener('click', (event) => {
    const opener = event.target.closest('[data-appearance-open]');
    if (opener) {
      event.preventDefault();
      const panel = opener.closest('.menu-panel');
      if (panel) showPage(panel, true);
      return;
    }
    const back = event.target.closest('[data-appearance-back]');
    if (back) {
      event.preventDefault();
      const panel = back.closest('.menu-panel');
      if (panel) showPage(panel, false);
      return;
    }
    const option = event.target.closest('button[data-appearance-option]');
    if (option) {
      event.preventDefault();
      choose(option.dataset.appearanceOption);
    }
  });

  /* Reopening the menu starts from its first page, as YouTube's does. */
  document.addEventListener('toggle', (event) => {
    const menu = event.target;
    if (!(menu instanceof HTMLDetailsElement) || menu.open) return;
    const panel = menu.querySelector(':scope > .menu-panel[data-view]');
    if (panel) delete panel.dataset.view;
  }, true);
})();
