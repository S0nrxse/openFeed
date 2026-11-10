# Accounts and sign-in

How OpenFeed's accounts, sign-in methods and sessions work, and how to add a sign-in
method. Code: `app/openfeed/accounts.py` (storage), `signin.py` (flow, sessions, method
registry, request safety), `account_pages.py` (pages and forms), `passwords.py`,
`twofactor.py`, `ratelimit.py`, `accounts.sql` (tables), `tools/accounts.py` (CLI).
Phase 2 methods: `passkeys.py` (+ `assets/auth/passkeys.js`), `oidc.py` (any number of
OIDC providers), `trusted_header.py` (forward-auth proxies), `authstate.py` (single-use
challenges and flows kept on the server).

## Modes

`OPENFEED_AUTH_MODE` picks one:

| Mode | Who you are | Sign-in |
| --- | --- | --- |
| `disabled` (default) | everyone is the `local` user | none; for one person on a trusted network. Unchanged from before accounts. |
| `accounts` | your account | built-in: username and password, optional 2FA, passkeys. Signing in is always required. Every configured OIDC provider appears as a button; a trusted proxy header signs people in when configured. |
| `authentik_oidc` | your account | the configured OIDC provider(s). With one provider and nothing else, `/auth/login` still redirects straight to it, so existing deployments need no change. With more methods it shows the sign-in page. `OPENFEED_PASSWORD_SIGNIN=true` adds the password form, `OPENFEED_PASSKEYS=on` passkeys. |

Every person's history, likes, playlists, preferences, downloads and tokens are keyed by
their account's `user_key`, which never changes. Household profiles hang under an account
as before (`account_profiles`).

## Data model (`accounts.sql`)

- `accounts`: `id`, `user_key` (unique, the join key for all data), `username` (unique,
  lowercase), `display_name`, `role` (`admin` | `member`), `disabled`, TOTP columns,
  `created_at`, `last_active_at`. No email address.
- `account_identities`: one row per linked sign-in method. `method` is `password`,
  `passkey`, `oidc:<provider>` or `header:<header name>`; `subject` identifies the
  credential inside the method (the account id for a password, the credential id for a
  passkey, the provider's `sub`, the name the proxy sends); `data` holds what is needed to
  verify it (the Argon2id hash for a password; a passkey's public key, counter and user
  handle). `label` is a passkey's name. Unique on `(method, subject)`. No schema change
  was needed for phase 2.
- `account_recovery_codes`: HMAC-SHA256 of each code (keyed with the server secret), `used_at`.
- `auth_sessions`: SHA-256 of the session token, account, created, last seen, idle and
  absolute expiry, the time of the last confirmation (`reauth_at`), a coarse device label
  ("Firefox on Android"), the methods used (`password`, `totp`, `passkey` plus
  `passkey-uv` when the authenticator verified the user, `oidc:<name>`, `header:<name>`),
  and the IdP groups at sign-in. No IP address.
- `account_links`: single-use invite and reset links (hash of the token, expiry, used_at).
- `app_settings`: install-wide settings changed in the app (registration mode, whether the
  pre-sign-in data was taken over).

Accounts made from an Authentik sign-in keep the Authentik `sub` as their `user_key`, so
their data from before accounts needs no migration. At startup every earlier Authentik user
(a `users` row with `source = authentik`) gets an account row (`backfill_oidc_accounts`).

## Sessions

- The cookie (`OPENFEED_SESSION_COOKIE`, default `openfeed_session`) holds `v2.<random>`.
  It is HttpOnly, SameSite=Lax, and Secure when `OPENFEED_PUBLIC_URL` is https
  (`OPENFEED_COOKIE_SECURE` overrides). Only its SHA-256 is stored.
- Every request looks the session up once (cached on the request). Expired, idle,
  revoked and disabled-account sessions stop at once. A lookup failure reads as signed out.
- Lifetimes: `OPENFEED_SESSION_IDLE_SECONDS` (default 7 days without use) and
  `OPENFEED_AUTH_SESSION_TTL_SECONDS` (default 30 days from sign-in).
- A new token is issued on every sign-in (an existing session cookie on that browser is
  ended), after "Confirm it's you", after a password change and when 2FA is turned on or
  off. When an admin changes someone's role, that person's sessions end.
- Signed-in people see their devices in Account & security and can sign out one, or all
  but this one. A password change signs out the others. Labels stay coarse ("Firefox on
  Android"; nothing finer is stored); each row also says when it signed in (in the viewer's
  time zone), "This device" comes first, and a four-character tag from the session's row id
  is the same on every device's list, so two "Chrome on Linux" rows can be told apart.
- Before accounts, the session was a stateless signed cookie. With sign-in off it is still
  honoured, as before. In `authentik_oidc` mode such a cookie is exchanged once for a server
  session (`signin.exchange_legacy_cookie`), so nobody is signed out by the upgrade; in
  `accounts` mode it means nothing.

## Request safety

- **CSRF:** `signin.csrf_failure`, called from `main.harden_http`, refuses a POST, PUT,
  PATCH or DELETE that carries a server session cookie, or targets `/auth/`, `/setup`,
  `/account` or `/admin/users`, unless its `Origin` (or `Referer`, when a browser drops
  Origin) is this site. Browsers send Origin on every such request; scripts use tokens and
  send no cookie. Plus SameSite=Lax.
- **Re-authentication:** password changes, 2FA, recovery codes, sign-in methods (adding a
  passkey, linking a provider or a proxy name), deleting an account and every admin action
  need a confirmation in the last `OPENFEED_REAUTH_SECONDS` (default 600). The pages say so
  and link to `/auth/confirm`, which offers the password, "Confirm with a passkey"
  (user verification required), each linked OIDC provider (`prompt=login`, `max_age=0`,
  and `auth_time` must be fresh) and a linked trusted proxy name.
- **Answers to a POST:** a page drawn as the answer to a form (a refused password, recovery
  codes, an admin's one-time reset or invite link, a refused setting) carries the GET address
  it belongs at, and `assets/auth/replace-url.js` puts it in the address bar
  (`account_pages.replace_url_tag`), so Reload does not send the form again. The address
  never holds a secret: one-time codes and links stay only in the page. Everything else
  redirects (post/redirect/get) with a fixed `?done=` or `?error=` key. A wrong current
  password re-draws Account & security with the error under that field.
- **Generic errors:** a wrong password, an unknown username and a disabled account all get
  "That username and password don't match an active account." An unknown username still
  spends one Argon2 verification, so timing does not tell them apart either.
- **Rate limits** (Redis, short TTLs, `ratelimit.py`): failed sign-ins per typed username
  (8 per 15 minutes; an unknown name locks the same way, so this reveals nothing) and per
  client (25 per 15 minutes); 2FA codes per account (6 per 15 minutes); setup codes, password
  confirmations and sign-ups. Client keys are an HMAC of the address keyed with the server
  secret, never the address. Without Redis the counters fall back to process memory. Behind
  a reverse proxy, list it in `OPENFEED_TRUSTED_PROXIES` so the client address comes from
  `X-Forwarded-For`.

## Passwords and 2FA

- Argon2id via argon2-cffi with its default parameters; a sign-in rehashes when they
  change. At least 10 characters, at most 256, not one of a built-in list of very common
  passwords, not the username. No composition rules, no expiry.
- TOTP (RFC 6238, 6 digits, 30 s) via pyotp, one step of clock skew either way. A code works
  once: the step of the last accepted code is stored and a code must be from a later step
  (`accounts.claim_totp_step`, one UPDATE). The QR code is drawn on the server as inline SVG
  by segno; nothing is fetched from anywhere.
- Ten recovery codes, shown once, each usable once, stored as keyed hashes. The codes page
  is the answer to the POST that made them and never puts them in a URL: its address bar
  moves to `/account?done=2fa_on` (or `done=codes`), so Reload or Back shows the account page
  with a notice, not the codes again and not a new set.
- The TOTP secret itself is stored in the database as base32 (it has to be readable to
  check codes). Someone with a copy of the database still needs the password.

## First run and registration

- In `accounts` mode with no admin, every page leads to `/setup`, which asks for a one-time
  setup code printed to the server log at startup
  (`docker compose logs openfeed-app | grep "setup code"`), or set `OPENFEED_SETUP_CODE`.
  The code is shared between processes through Redis and forgotten once an admin exists.
- After setup, "Keep the data from before sign-in" moves everything the `local` user and
  its profiles own to the admin (`accounts.adopt_local_data`): one transaction, idempotent,
  rows that would collide with the admin's own keep the admin's. Profiles keep their data
  under keys renamed to `<admin key>:p:<id>`. API tokens made while the server was open are
  not moved. The offer stays in Admin, Users until it is taken or declined. The move includes
  `local`'s part of the shared catalog (`video_provenance`, and `catalog_claims`: when sign-in
  is first turned on on an install only `local` had used, `local` claims the catalog as it
  stood; see PRIVACY.md, "Shared catalog"), so the admin keeps the Home they had.
- Registration (Admin, Users): **closed** (only admins add people), **invite links**
  (default; single-use, 1, 7 or 30 days) or **open** sign-up (rate-limited per client and per
  hour for the whole server, a hidden honeypot field, no captcha, no email). The same
  setting decides whether a first sign-in through an OIDC provider or the trusted header
  may create an account (unless that method is set to `always`); an invite link offers the
  configured providers next to the password form.

## Lockout recovery without email

1. A recovery code at the 2FA step.
2. An admin's one-time reset link (Admin, Users, Manage, Password reset link; 24 hours).
   Using it signs that account out everywhere.
3. On the server: `python -m openfeed.tools.accounts` with `list`, `reset-password USER`
   (`--link` prints a reset link instead, `--password-stdin` for scripts), `create-admin
   USER`, `make-admin USER`, `reset-2fa USER`, `enable USER`, `disable USER`,
   `sign-out USER`. With Docker: `docker compose exec openfeed-app python -m openfeed.tools.accounts list`.

## Passkeys

Code: `passkeys.py`, `assets/auth/passkeys.js` (served on its own as
`/assets/auth/passkeys-<hash>.js`, only on the sign-in, "Confirm it's you" and Account &
security pages). Library: py_webauthn (`webauthn`).

- **Where they work.** The relying party ID is the host of `OPENFEED_PUBLIC_URL` and the
  expected origin is its scheme, host and port. Browsers offer passkeys only in a secure
  context and only for domain names, so passkeys are off when the public URL is plain http
  on anything but `localhost`, or an IP address (a LAN IP, say). Account & security then
  says why. In the browser the script also hides the buttons, with one sentence, when
  `PublicKeyCredential` is missing, the page is not a secure context, or the page is open
  at another address than the public URL.
- **Switch.** `OPENFEED_PASSKEYS=auto` (default): on wherever password sign-in is on
  (accounts mode). `on`: also in `authentik_oidc` mode. `off`.
- **Adding one** (Account & security, Passkeys, "Add a passkey"; needs a fresh "Confirm
  it's you"): `POST /account/passkeys/options` returns creation options (attestation
  `none`, resident key `preferred`, user verification `preferred`, the account's existing
  passkeys excluded), `POST /account/passkeys/register` verifies the answer and stores the
  passkey. Each passkey can be renamed and removed; the list shows when it was added and
  last used.
- **Signing in.** "Sign in with a passkey" with an empty username field is usernameless
  (discoverable credentials: the browser lists this site's passkeys). With a username typed,
  the options name only that account's passkeys, and the answer must be one of them. Where
  the browser supports conditional mediation, the username field is
  `autocomplete="username webauthn"` and the browser offers passkeys in its autofill. An
  unknown username gets the same answer as a username without passkeys.
- **Challenges** are 64 random bytes made by py_webauthn, kept on the server
  (`authstate`, Redis, 5 minutes) under a random flow id, and taken once: a second
  verify with the same flow, an expired flow, or a response to another challenge fails.
- **Stored** per passkey (`account_identities`, method `passkey`, subject = credential id):
  the COSE public key, the signature counter, the account's WebAuthn user handle (random,
  one per account, not the user_key), transports, AAGUID, backup flags. Never a secret.
- **Counters.** When the stored or the new counter is non-zero the new one must be
  larger; otherwise the sign-in is refused and logged as a possible cloned authenticator
  (py_webauthn's rule). Synced passkeys always report 0, which passes.
- **Two-step.** A passkey sign-in with user verification counts as both factors
  (`complete_first_factor(..., both_factors=True)`, session methods `passkey`,
  `passkey-uv`). Without user verification it is one factor and an account with TOTP gets
  the code step. "Confirm it's you" with a passkey requires user verification.
- **Passkey-only accounts.** Add a passkey, then remove the password. Removing anything is
  refused while it would leave no usable way in (`signin.can_remove`: another linked
  method that is available on this install now must remain; a provider that is no longer
  configured does not count).
- **Permissions-Policy** grants `publickey-credentials-get` and `-create` to `self` only
  (`privacy.permissions_policy`), never to a frame from elsewhere.
- **Rate limits.** Options only mint a short-lived, single-use challenge, and every sign-in
  page load asks for one (the browser's autofill), so they are generous per client (600 per
  5 minutes, `passkeys.OPTIONS_CLIENT`) and the install caps the challenges it keeps at once
  (5000 per 5 minutes, `OPTIONS_GLOBAL`): a household or office behind one address does not
  lock itself out. The limits that matter are on the verify step: failed verifications count
  per account (8 per 15 minutes, `VERIFY_ACCOUNT`) and against the per-client sign-in limit
  (25 per 15 minutes, shared with passwords). When the page-load request is over a limit the
  server answers `200 {"quiet": true}` and autofill simply offers no passkey that time (no
  error, nothing in the console); a pressed "Sign in with a passkey" button still says why.

## OIDC providers

Code: `oidc.py`. Each configured provider is one `oidc.ProviderMethod` in the registry,
key `oidc:<name>`.

**Configuration.**

```env
OPENFEED_OIDC_PROVIDERS=authentik,pocketid
OPENFEED_OIDC_POCKETID_ISSUER=https://id.example.com
OPENFEED_OIDC_POCKETID_CLIENT_ID=...
OPENFEED_OIDC_POCKETID_CLIENT_SECRET=...
OPENFEED_OIDC_POCKETID_DISPLAY_NAME=Pocket ID            # optional; known names are filled in
OPENFEED_OIDC_POCKETID_SCOPES=openid email profile groups # default: openid email profile
OPENFEED_OIDC_POCKETID_ADMIN_GROUPS=openfeed-admins
OPENFEED_OIDC_POCKETID_REQUIRED_GROUP=                   # optional
OPENFEED_OIDC_POCKETID_CREATE_ACCOUNTS=registration      # or always
# Optional: _GROUPS_CLAIM (groups), _REDIRECT_URI, _DISCOVERY_URL, _BACKCHANNEL_BASE_URL,
# _TOKEN_AUTH (client_secret_basic | client_secret_post | none; default from discovery)
```

`<NAME>` is the name upper-cased with `-` as `_`. Names are 1 to 32 letters, digits, `-`
and `_`. A listed provider without an issuer or client id is skipped with a log line. With
Docker, put the per-provider lines in `signin.env` next to `compose.yml` (read through
`env_file`, ignored by git); the fixed settings are passed through `compose.yml`.

**The original single provider.** `OPENFEED_OIDC_ISSUER`, `_CLIENT_ID`, `_CLIENT_SECRET`,
`_DISCOVERY_URL`, `_BACKCHANNEL_BASE_URL`, `_REDIRECT_URI`, `_SCOPE`, `OPENFEED_OIDC_NAME`
and the `AUTHENTIK_OIDC_*` spellings keep making one provider called `authentik`: key
`oidc:authentik` (existing identities stay linked), callback `/auth/callback`, admin groups
`OPENFEED_ADMIN_GROUPS`, required group `OPENFEED_REQUIRED_GROUP`, token request with
`client_secret_post` as before. Accounts it creates keep the provider's `sub` as user_key,
and an account whose user_key equals a `sub` (made by an Authentik sign-in before
accounts) is linked on first sign-in. Listing `authentik` in `OPENFEED_OIDC_PROVIDERS`
keeps those values as its defaults; `OPENFEED_OIDC_AUTHENTIK_*` override them one by one.
Its account-creation default is `always` in `authentik_oidc` mode (unchanged) and
`registration` in accounts mode (`OPENFEED_OIDC_CREATE_ACCOUNTS` overrides).

**Redirect URIs** to register at each provider: `<OPENFEED_PUBLIC_URL>/auth/oidc/<name>/callback`
(the `authentik` provider: `/auth/callback`).

**The flow** (authorization code with PKCE):

1. A sign-in page button is a plain link, `GET /auth/oidc/<name>/start?next=...` (the page
   CSP has `form-action 'self'`). It makes a random `state` (32 bytes), `nonce` (24 bytes)
   and PKCE verifier (48 bytes), stores them with the provider, intent, next path, account
   (for link and confirm) and invite on the server (`authstate`, 10 minutes, single use),
   sets an HttpOnly cookie holding a signed hash of the state (binds the flow to this
   browser), and redirects with `code_challenge_method=S256`.
2. `GET /auth/oidc/<name>/callback` (or `/auth/callback`) takes the flow by its state
   before anything else, so a replayed or double-submitted callback finds nothing and never
   reaches the token endpoint; checks the cookie, that the provider in the path is the one
   the flow started with, and an `iss` parameter when the provider sends one (RFC 9207).
3. The code is exchanged with the verifier. The ID token must be signed (RS/PS/ES/EdDSA
   against the provider's JWKS, fetched by `jwks_uri` and cached an hour, refetched at most
   once a minute for an unknown `kid`; or HS256/384/512 with the client secret, which is how
   Authentik signs when no key is chosen; never `none`), with `iss` the configured issuer,
   `aud` the client id (`azp` too when present or when there are several audiences), not
   expired (60 s leeway), and `nonce` equal to the flow's.
4. Claims come from the verified ID token. If the provider has a userinfo endpoint it is
   asked as well (groups and names are often only there) and its `sub` must equal the ID
   token's. **Why the ID token rather than userinfo alone, as before:** userinfo answers
   anyone holding the access token and says nothing about this browser's request; the
   signed ID token carries this flow's nonce, names this client and its issuer.
   For `max_age=0` re-confirmation, `auth_time` must not be older than the flow.

**Accounts.** A known identity signs in (2FA is still asked when the account has it). An
unknown one gets a new account only when registration is open (rate-limited like sign-up),
the person came through an invite link (`/auth/oidc/<name>/start?invite=<token>`, offered
on the invite page; the invite is used up in the same transaction), or the provider has
`_CREATE_ACCOUNTS=always`. Otherwise the page says "Sign-in isn't open" and suggests
signing in another way and linking. Never matched to an existing account by email address
or username; a new account takes a free username derived from `preferred_username` and a
random user_key (not the `sub`, except for the original provider as above).

**Linking.** Account & security, Sign-in methods, "Link <provider>" (needs a fresh
"Confirm it's you"). The flow is bound to the account that started it; an identity already
linked to another account is refused.

**Groups.** `_GROUPS_CLAIM` (default `groups`; the original provider also reads
`ak_groups`). A list, a comma/space separated string, or an object (Zitadel's role claim:
its keys) all work. Admin and required groups are per provider (`signin.idp_policy`).

## Trusted header (forward-auth proxies)

Code: `trusted_header.py`, called from `main.harden_http` before anything reads the user.

- `OPENFEED_TRUSTED_HEADER=Remote-User` (Authelia, Authentik's proxy outpost:
  `X-authentik-username`, oauth2-proxy: `X-Forwarded-User` / `X-Auth-Request-User`,
  Cloudflare Access: `Cf-Access-Authenticated-User-Email`), with optional
  `OPENFEED_TRUSTED_HEADER_GROUPS` and `OPENFEED_TRUSTED_HEADER_NAME` headers.
- Believed only when the **direct peer** (the TCP connection, `request.client`) is in
  `OPENFEED_TRUSTED_PROXIES`. From any other peer the configured headers are removed from
  the request before anything reads it. `X-Forwarded-For` does not make a peer trusted. If
  uvicorn itself rewrites `request.client` from forwarded headers (`--proxy-headers`), list
  the address uvicorn reports.
- The header value is the identity's subject under `header:<header name>`. A known name
  starts a normal server-side session (methods `header:<name>`, groups from the groups
  header) and the session cookie carries the visit. When the proxy reports another name,
  the old session ends and a new one starts. A browser already signed in another way keeps
  its session; the person can link the reported name in Account & security.
- An unknown name gets an account only when registration is open, or always with
  `OPENFEED_TRUSTED_HEADER_CREATE_ACCOUNTS=always`. Otherwise the sign-in page explains.
- The proxy authenticates, including any second factor; OpenFeed does not ask for its own
  code on a header sign-in. "Confirm it's you" accepts a POST that arrives through the
  trusted proxy with a name linked to this account.
- Sign-out: set `OPENFEED_TRUSTED_HEADER_LOGOUT_URL` to the proxy's sign-out page, or the
  proxy signs you straight back in.
- Admin groups: `OPENFEED_TRUSTED_HEADER_ADMIN_GROUPS`, else `OPENFEED_ADMIN_GROUPS`.
  `OPENFEED_REQUIRED_GROUP` applies.

## The mobile app (Capacitor, Android WebView)

- Passkeys: the app's WebView has no working WebAuthn in the current build. The sign-in
  page's script hides the passkey buttons when `PublicKeyCredential` is missing, and also
  when the user agent is the Android app (`OpenFeedApp` + `Android`), where the API may be
  present but does not complete; one sentence says to sign in another way or use a browser.
  The password and provider buttons are unaffected.
- OIDC still runs inside the WebView, as before: the button is a plain link to the
  provider. The app loads a provider's page in the app only when its host is the server or
  a listed sign-in host (mobile/README.md, "Signing in"); otherwise it opens the system
  browser and the session lands there. In the app the sign-in page names the providers'
  hosts to add under App settings, Sign-in hosts.

## Adding a sign-in method

A method is a `signin.SignInMethod` registered with `signin.register(instance)`, or, when
its instances depend on configuration, kept current by a loader registered with
`signin.register_loader(fn)` (oidc.py and trusted_header.py do this; loaders run before
every registry read):

```python
class ExampleMethod(signin.SignInMethod):
    def __init__(self):
        super().__init__(key="example", kind="example", label="Example", both_factors=False)

    def available(self) -> bool: ...                      # configured here?
    def entry(self, next_path) -> signin.MethodEntry | None: ...    # sign-in page button
    def describe(self, identity) -> dict: ...             # row in "Sign-in methods"
    def can_link(self, account, identities) -> bool: ... # offer "Link" in Account & security
    def link_href(self, next_path="/account") -> str: ...
    def reauth_entry(self, identities, next_path) -> signin.MethodEntry | None: ...  # "Confirm it's you"
    def remove(self, account, identity) -> bool: ...     # default deletes the identity row
    def group_policy(self) -> tuple[frozenset[str], str]: ...  # IdP methods: (admin groups, required group)
```

Rules a method follows:

- Sign-in: look up `accounts.find_identity(key, subject)`, refuse a disabled account, and
  return `signin.complete_first_factor(request, account, key, next_path, idp_groups=...,
  both_factors=...)`. That asks for 2FA when the account has it and the method does not
  count as both factors, and otherwise starts the session.
- Link: require `signin.reauth_fresh(user)`, then `accounts.add_identity(account_id, key,
  subject, label, data)`. Never link by matching an email address or username.
- Confirm it's you: check the identity belongs to the current account, then
  `signin.rotate(request, user, response, reauth=True)`.
- Keep ceremony state on the server with `authstate.put/take` (single use, short TTL).
- Routes go under `/auth/<method>/...` or `/account/...` so the CSRF check covers their
  POSTs; add the router's routes in main.py next to the others.
- Store only verifiers in `account_identities.data` (public keys, counters), never secrets.
- The sign-in page lists `entry()` of every available non-password method automatically
  (`kind` `passkey` renders a script button, `header` a form, anything else a link).

## Admin rights

`signin.is_admin`: the account's role, or for a session from an IdP, membership of that
provider's admin groups within `OPENFEED_ADMIN_SESSION_SECONDS` of sign-in
(`signin.idp_policy`: `OPENFEED_OIDC_<NAME>_ADMIN_GROUPS` per provider;
`OPENFEED_ADMIN_GROUPS` for the single-provider settings; `OPENFEED_TRUSTED_HEADER_ADMIN_GROUPS`,
else `OPENFEED_ADMIN_GROUPS`, for the trusted header). A group name at one provider never
grants anything at another. In `authentik_oidc` mode with no admin groups configured,
everyone signed in through the original provider administers, as before. Admin pages, with tabs between them: `/admin/server` (install-wide settings,
`admin_pages.py`), `/admin/users` (accounts, roles, disable, reset link, reset 2FA, sign
out, delete with all data, invites, registration mode) and `/admin/status`. The last active
admin cannot be demoted, disabled or deleted. With sign-in off the one local user is the
admin of `/admin/server` and `/admin/status`.

An API token minted by an admin carries admin rights only while its account is still an
admin (accounts mode): demoting or disabling the account takes them away from its tokens.

### Install-wide settings and member limits

- `instance.py` keeps the settings Admin → Server changes, in one `app_settings` row
  (`instance`) over the `.env` defaults, cached per process for 10 seconds. A few
  `config.Settings` fields (RYD, SponsorBlock, DeArrow, YouTube player, public instances)
  read through it, so existing code sees the admin's choice through `get_settings()`.
- `policy.py` is asked once per request by `main.enforce_api_access`: install-wide routes
  (`policy.ADMIN_ONLY`: syncs, enrichment, discovery, vector rebuilds, RYD batches, playback
  probes, status, `/api/admin/*`) answer 403 `admin_required` to members, and the expensive
  routes (`policy.LIMITED`) count against the account's rate limits. Both apply only with
  sign-in on; admins are not limited.
- Add a route that changes the server: list it in `policy.ADMIN_ONLY`. Add an expensive
  route: list it in `policy.LIMITED` under an existing `instance.LIMITS` key.
- `tests/test_isolation.py` signs in two members and an admin and tries each cross-user
  access; extend it with every new route that takes an id.

## API tokens

Tokens belong to the `user_key` that made them. With sign-in on, a token works only while
its account is active: disabling stops it at once, deleting the account deletes it. In
`accounts` mode a token whose owner has no account is refused.
