/* Admin, Server (admin_pages.py): Test all sources and Diagnose playback. The rest of the
   page is plain forms that work without this. */
(() => {
  'use strict';

  const page = document.querySelector('[data-admin-page]');
  if (!page) return;
  const results = page.querySelector('[data-admin-results]');

  function row(ok, title, detail, time) {
    const item = document.createElement('div');
    item.className = `admin-result ${ok ? 'is-ok' : 'is-bad'}`;
    const dot = document.createElement('span');
    dot.className = 'admin-result-dot';
    dot.setAttribute('aria-hidden', 'true');
    const copy = document.createElement('div');
    const strong = document.createElement('strong');
    strong.textContent = `${title}: ${ok ? 'works' : 'failed'}`;
    copy.append(strong);
    if (detail) {
      const small = document.createElement('small');
      small.textContent = detail;
      copy.append(small);
    }
    const when = document.createElement('span');
    when.className = 'admin-result-time';
    when.textContent = time;
    item.append(dot, copy, when);
    return item;
  }

  function note(text) {
    const item = document.createElement('div');
    item.className = 'admin-result';
    const copy = document.createElement('div');
    copy.textContent = text;
    item.append(document.createElement('span'), copy);
    return item;
  }

  async function run(button, url, method, render) {
    if (!results || button.disabled) return;
    const label = button.textContent;
    button.disabled = true;
    button.textContent = 'Running…';
    results.replaceChildren(note('Working. This can take a few seconds per source.'));
    try {
      const response = await fetch(url, { method, headers: { accept: 'application/json' } });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.message || data.detail || `HTTP ${response.status}`);
      results.replaceChildren(...render(data));
    } catch (error) {
      results.replaceChildren(note(`Could not run: ${error.message}`));
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }

  page.querySelector('[data-admin-test]')?.addEventListener('click', (event) => {
    run(event.currentTarget, '/api/playback/sources/test', 'POST', (data) => {
      const items = (data.results || []).map((item) => row(item.ok, item.label || item.id, item.detail || '', `${item.latency_ms || 0} ms`));
      return items.length ? items : [note('No sources are enabled.')];
    });
  });

  page.querySelector('[data-admin-diagnose]')?.addEventListener('click', (event) => {
    run(event.currentTarget, '/api/playback/diagnose', 'GET', (data) => {
      const diagnosis = data.diagnosis || {};
      const steps = (diagnosis.steps || []).map((step) => row(step.ok, step.name, step.detail || '', `${step.latency_ms || 0} ms`));
      const summary = diagnosis.message || diagnosis.detail || (diagnosis.ok ? 'Every hop answered.' : '');
      return summary ? [...steps, note(summary)] : steps;
    });
  });
})();
