/* Passkeys: sign in, confirm it's you, and add one (passkeys.py serves this file on its own
   to the sign-in, "Confirm it's you" and Account & security pages).

   Everything degrades. Without PublicKeyCredential (older browsers, the OpenFeed Android
   app's WebView), outside a secure context, or on an address other than the server's
   public one, the passkey buttons stay hidden and one sentence says why. The rest of the
   page (password form, provider buttons) works as before. */
(() => {
  if (window.__openfeedPasskeys) return;
  window.__openfeedPasskeys = true;

  const root = document.querySelector('[data-passkeys]');
  if (!root) return;
  const GENERIC = "That passkey didn't work here. Try again, or sign in another way.";

  const toBuffer = (value) => {
    const base64 = String(value).replace(/-/g, '+').replace(/_/g, '/');
    const binary = atob(base64 + '='.repeat((4 - (base64.length % 4)) % 4));
    const out = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i);
    return out.buffer;
  };
  const toBase64url = (buffer) => {
    const bytes = new Uint8Array(buffer);
    let binary = '';
    for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
    return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  };

  function unsupportedReason() {
    if (root.dataset.passkeys !== 'on') return root.dataset.passkeyReason || '';
    const ua = navigator.userAgent || '';
    if (/OpenFeedApp/.test(ua) && /Android/.test(ua)) {
      return "Passkeys don't work inside the OpenFeed Android app yet. Sign in another way here, or open OpenFeed in a browser.";
    }
    const origin = (root.dataset.passkeyOrigin || '').toLowerCase();
    if (origin && location.origin.toLowerCase() !== origin) {
      return `Passkeys for this server work at ${origin}. This page is open at ${location.origin}.`;
    }
    if (!window.isSecureContext) return 'Passkeys need a secure (https) connection, and this page is not on one.';
    if (typeof window.PublicKeyCredential === 'undefined' || !navigator.credentials) {
      return "This browser doesn't support passkeys.";
    }
    return '';
  }

  const requestOptions = (pk) => {
    const out = { ...pk, challenge: toBuffer(pk.challenge) };
    if (Array.isArray(pk.allowCredentials)) out.allowCredentials = pk.allowCredentials.map((c) => ({ ...c, id: toBuffer(c.id) }));
    return out;
  };
  const creationOptions = (pk) => {
    const out = { ...pk, challenge: toBuffer(pk.challenge), user: { ...pk.user, id: toBuffer(pk.user.id) } };
    if (Array.isArray(pk.excludeCredentials)) out.excludeCredentials = pk.excludeCredentials.map((c) => ({ ...c, id: toBuffer(c.id) }));
    return out;
  };
  const credentialJSON = (credential) => {
    const r = credential.response;
    const out = {
      id: credential.id,
      rawId: toBase64url(credential.rawId),
      type: credential.type,
      clientExtensionResults: credential.getClientExtensionResults ? credential.getClientExtensionResults() : {},
      response: { clientDataJSON: toBase64url(r.clientDataJSON) },
    };
    if (credential.authenticatorAttachment) out.authenticatorAttachment = credential.authenticatorAttachment;
    if (r.attestationObject) {
      out.response.attestationObject = toBase64url(r.attestationObject);
      if (typeof r.getTransports === 'function') out.response.transports = r.getTransports();
    }
    if (r.authenticatorData) {
      out.response.authenticatorData = toBase64url(r.authenticatorData);
      out.response.signature = toBase64url(r.signature);
      if (r.userHandle && r.userHandle.byteLength) out.response.userHandle = toBase64url(r.userHandle);
    }
    return out;
  };

  async function post(url, body) {
    const response = await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'content-type': 'application/json', accept: 'application/json' },
      body: JSON.stringify(body),
    });
    let data = {};
    try { data = await response.json(); } catch (error) { data = {}; }
    return { ok: response.ok, status: response.status, data };
  }

  const explain = (error) => {
    if (!error) return GENERIC;
    if (error.name === 'NotAllowedError') return 'The passkey request was cancelled or timed out.';
    if (error.name === 'InvalidStateError') return 'This device already has a passkey for your account.';
    if (error.name === 'SecurityError') return 'The browser refused: passkeys need this server’s own https address.';
    return error.message || GENERIC;
  };

  const notes = Array.from(document.querySelectorAll('[data-passkey-note]'));
  const say = (message, isError) => {
    notes.forEach((note) => {
      note.textContent = message || '';
      note.hidden = !message;
      note.classList.toggle('is-error', Boolean(isError));
    });
  };
  const buttons = Array.from(document.querySelectorAll('[data-passkey-signin], [data-passkey-confirm], [data-passkey-add-button]'));

  const reason = unsupportedReason();
  if (reason) {
    buttons.forEach((button) => { button.hidden = true; button.disabled = true; });
    document.querySelectorAll('[data-passkey-add] input[name="name"]').forEach((input) => { input.hidden = true; });
    if (buttons.length) say(reason, false);
    return;
  }
  buttons.forEach((button) => { button.hidden = false; button.disabled = false; });

  // --- sign in and confirm it's you -----------------------------------------------------
  let conditional = null;
  let conditionalRun = null;

  async function getPasskey({ intent = 'signin', username = '', next = '/', mediation = '' }) {
    const quietly = mediation === 'conditional';
    const options = await post('/auth/passkey/options', { intent, username, next, conditional: quietly });
    // The page-load (autofill) request: when the server is busy it answers {quiet: true},
    // and autofill just doesn't offer passkeys this time. The button still works.
    if (quietly && (!options.ok || options.data.quiet || !options.data.publicKey)) return;
    if (!options.ok) throw new Error(options.data.error || "Passkeys aren't available right now.");
    const args = { publicKey: requestOptions(options.data.publicKey) };
    if (mediation === 'conditional') {
      conditional = new AbortController();
      args.mediation = 'conditional';
      args.signal = conditional.signal;
    }
    const credential = await navigator.credentials.get(args);
    if (!credential) throw new Error(GENERIC);
    const done = await post('/auth/passkey/verify', { flow: options.data.flow, credential: credentialJSON(credential) });
    if (!done.ok) throw new Error(done.data.error || GENERIC);
    location.assign(done.data.redirect || next || '/');
  }

  const usernameField = document.querySelector('input[autocomplete~="webauthn"]');
  const signinButton = document.querySelector('[data-passkey-signin]');

  async function stopConditional() {
    if (!conditional) return;
    conditional.abort();
    conditional = null;
    if (conditionalRun) await conditionalRun.catch(() => {});
  }

  if (signinButton) {
    const next = signinButton.dataset.next || '/';
    signinButton.addEventListener('click', async () => {
      signinButton.disabled = true;
      say('');
      await stopConditional();
      try {
        await getPasskey({ username: usernameField ? usernameField.value : '', next });
      } catch (error) {
        say(explain(error), true);
        signinButton.disabled = false;
      }
    });
    // Autofill: the browser lists this site's passkeys under the username field.
    if (usernameField && typeof PublicKeyCredential.isConditionalMediationAvailable === 'function') {
      PublicKeyCredential.isConditionalMediationAvailable().then((ok) => {
        if (!ok) return;
        conditionalRun = getPasskey({ next, mediation: 'conditional' }).catch((error) => {
          if (error && error.name !== 'AbortError' && error.name !== 'NotAllowedError') say(explain(error), true);
        });
      }).catch(() => {});
    }
  }

  document.querySelectorAll('[data-passkey-confirm]').forEach((button) => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      say('');
      try {
        await getPasskey({ intent: 'reauth', next: button.dataset.next || '/account' });
      } catch (error) {
        say(explain(error), true);
        button.disabled = false;
      }
    });
  });

  // --- add a passkey ---------------------------------------------------------------------
  document.querySelectorAll('[data-passkey-add]').forEach((form) => {
    const button = form.querySelector('[data-passkey-add-button]');
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (button) button.disabled = true;
      say('');
      const name = (form.querySelector('input[name="name"]') || {}).value || '';
      try {
        const options = await post('/account/passkeys/options', { name });
        if (!options.ok) {
          if (options.data.redirect) { location.assign(options.data.redirect); return; }
          throw new Error(options.data.error || "Passkeys aren't available right now.");
        }
        const credential = await navigator.credentials.create({ publicKey: creationOptions(options.data.publicKey) });
        if (!credential) throw new Error(GENERIC);
        const done = await post('/account/passkeys/register', { flow: options.data.flow, name, credential: credentialJSON(credential) });
        if (!done.ok) throw new Error(done.data.error || GENERIC);
        location.assign(done.data.redirect || '/account');
      } catch (error) {
        say(explain(error), true);
        if (button) button.disabled = false;
      }
    });
  });
})();
