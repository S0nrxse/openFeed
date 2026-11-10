/* Settings > Privacy (main._privacy_pane). Two kinds of switch:
   - data-privacy-choice: a third-party player the browser would load (YouTube, public
     Invidious). Saved to /api/privacy, which refuses one the admin has not allowed.
   - data-privacy-feed: a lookup this server makes (SponsorBlock, DeArrow, RYD). These
     already have a switch under Integrations, so this one drives that switch, and both
     stay in step whichever is pressed. */
(() => {
  const pane = () => document.querySelector('[data-settings-pane-body="privacy"]');
  const toast = (message, kind) => { if (window.ofToast) window.ofToast(message, { kind }); };

  function setSummary(text) {
    const node = document.querySelector('[data-privacy-summary]');
    if (node && text) node.textContent = text;
  }
  function localSummary() {
    const root = pane();
    if (!root) return;
    const on = [...root.querySelectorAll('[data-privacy-service]')]
      .filter((row) => row.querySelector('input.switch')?.checked)
      .map((row) => row.querySelector('strong')?.textContent || '');
    setSummary(on.length ? `On for you: ${on.join(', ')}.` : 'Nothing outside this server is on. Your browser talks only to this server.');
  }
  function markRow(input) {
    const row = input.closest('[data-privacy-service]');
    if (row) row.dataset.state = input.checked ? 'on' : 'off';
  }

  document.addEventListener('change', async (event) => {
    const input = event.target.closest('[data-privacy-choice]');
    if (!input) return;
    const key = input.dataset.privacyChoice;
    const on = input.checked;
    input.disabled = true;
    try {
      const response = await fetch('/api/privacy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [key]: on }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
      setSummary(data.summary);
      const name = input.getAttribute('aria-label') || 'This player';
      toast(on ? `${name} on. It is offered only when you press its button.` : `${name} off.`, 'success');
    } catch (error) {
      input.checked = !on;
      toast(`Not saved: ${error.message || 'the server could not be reached'}`, 'error');
    } finally {
      input.disabled = false;
      markRow(input);
      input.focus({ preventScroll: true });
    }
  });

  /* A server lookup switch drives the Integrations switch it mirrors, so that switch's own
     save (and its reveal of the options under it) runs exactly once. */
  document.addEventListener('change', (event) => {
    const input = event.target.closest('[data-privacy-feed]');
    if (!input) return;
    const twin = document.querySelector(input.dataset.privacyMirror || '');
    if (!twin) return;
    twin.checked = input.checked;
    twin.dispatchEvent(new Event('change', { bubbles: true }));
    input.disabled = true;
  });

  /* Whichever switch was pressed, the twin's final state (after its save, or its revert on
     failure) is the truth. saveControl disables it while saving. */
  function follow(twin) {
    const mirrors = () => [...document.querySelectorAll('[data-privacy-feed]')]
      .filter((node) => node.dataset.privacyMirror && twin.matches(node.dataset.privacyMirror));
    new MutationObserver(() => {
      if (twin.disabled) return;
      mirrors().forEach((node) => {
        node.checked = twin.checked;
        node.disabled = false;
        markRow(node);
      });
      localSummary();
    }).observe(twin, { attributes: true, attributeFilter: ['disabled'] });
  }
  function init() {
    const root = pane();
    if (!root) return;
    const seen = new Set();
    root.querySelectorAll('[data-privacy-feed]').forEach((node) => {
      const twin = document.querySelector(node.dataset.privacyMirror || '');
      if (twin && !seen.has(twin)) { seen.add(twin); follow(twin); }
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
