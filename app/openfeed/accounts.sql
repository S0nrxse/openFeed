-- Accounts, sign-in methods and server-side sessions (accounts.py, docs/accounts.md).
-- Applied by db.init_db after schema.sql. Kept apart from schema.sql because none of these
-- tables hold per-profile data: deleting an account goes through accounts.delete_account,
-- which removes every user_key it owns from the tables in profiles.USER_KEY_TABLES.
--
-- user_key stays the join key for all data. An account owns exactly one user_key (its main
-- profile); household profiles under it are rows in account_profiles with account_key equal
-- to that user_key. Accounts created from an Authentik sign-in keep the sub as user_key, so
-- data saved before accounts existed stays attached.

CREATE TABLE IF NOT EXISTS accounts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_key TEXT NOT NULL UNIQUE,
  username TEXT NOT NULL,
  display_name TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
  disabled BOOLEAN NOT NULL DEFAULT false,
  -- TOTP: the secret (base32), the step of the last accepted code (replay guard) and a
  -- secret waiting for its first code during setup.
  totp_secret TEXT,
  totp_enabled_at TIMESTAMPTZ,
  totp_last_step BIGINT,
  totp_pending_secret TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_active_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_username ON accounts (lower(username));

-- One row per sign-in method linked to an account. method is 'password', 'passkey',
-- 'oidc:<provider>' or 'header:<name>'; subject identifies the credential within the
-- method (the IdP's sub, a passkey credential id, the account id for a password). data
-- holds what the method needs to verify it (a password hash, a public key), never a
-- plaintext secret.
CREATE TABLE IF NOT EXISTS account_identities (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  method TEXT NOT NULL,
  subject TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  data JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ,
  UNIQUE (method, subject)
);
CREATE INDEX IF NOT EXISTS idx_account_identities_account ON account_identities (account_id);

CREATE TABLE IF NOT EXISTS account_recovery_codes (
  account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  code_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  used_at TIMESTAMPTZ,
  PRIMARY KEY (account_id, code_hash)
);

-- Server-side sessions. The cookie carries a random id; only its SHA-256 is stored, so a
-- copy of this table cannot be replayed. No IP address is kept: device is a coarse label
-- such as "Firefox on Android", from the User-Agent at sign-in.
CREATE TABLE IF NOT EXISTS auth_sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  token_hash TEXT NOT NULL UNIQUE,
  account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  idle_expires_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  reauth_at TIMESTAMPTZ,
  device TEXT NOT NULL DEFAULT '',
  methods TEXT[] NOT NULL DEFAULT '{}',
  -- Groups the identity provider reported at sign-in (admin and access groups).
  idp_groups JSONB NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_account ON auth_sessions (account_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry ON auth_sessions (expires_at);

-- Single-use links: invites (kind 'invite') and admin password-reset links (kind 'reset').
-- Only a hash of the token in the link is stored.
CREATE TABLE IF NOT EXISTS account_links (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind TEXT NOT NULL CHECK (kind IN ('invite', 'reset')),
  token_hash TEXT NOT NULL UNIQUE,
  account_id UUID REFERENCES accounts(id) ON DELETE CASCADE,
  role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
  note TEXT NOT NULL DEFAULT '',
  created_by UUID REFERENCES accounts(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL,
  used_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_account_links_kind ON account_links (kind, created_at DESC);

-- Install-wide settings an admin changes in the app (registration mode, ...).
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
