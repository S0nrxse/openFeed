/* A page drawn as the answer to a form POST (a refused password, recovery codes, a one-time
   link) names the GET address it belongs at in data-replace-url. Swapping the address bar
   to it means Reload and Back fetch that page instead of sending the form again. The
   address never holds anything secret: one-time codes and links stay only in this page.
   account_pages.py serves this file on its own, for the signed-out pages too. */
(() => {
  const marker = document.querySelector('[data-replace-url]');
  const url = marker && marker.getAttribute('data-replace-url');
  if (!url || url.charAt(0) !== '/' || url.charAt(1) === '/' || !window.history || !history.replaceState) return;
  try { history.replaceState(history.state, '', url); } catch (error) { /* an old WebView: nothing to do */ }
})();
