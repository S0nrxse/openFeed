CREATE TABLE IF NOT EXISTS channels (
  id BIGSERIAL PRIMARY KEY,
  youtube_channel_id TEXT UNIQUE,
  title TEXT NOT NULL,
  feed_url TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS videos (
  id BIGSERIAL PRIMARY KEY,
  youtube_video_id TEXT UNIQUE NOT NULL,
  channel_id BIGINT REFERENCES channels(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  url TEXT,
  thumbnail_url TEXT,
  author TEXT,
  summary TEXT,
  published_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per feed OpenFeed polls, shared by everyone who follows it: a channel is
-- fetched once however many people subscribe. Who follows what is user_subscriptions.
-- (Installs from before 2026-11 had these two tables under other names; migrations.py
-- renames them at startup, before this file runs.)
CREATE TABLE IF NOT EXISTS feeds (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  feed_url TEXT,
  html_url TEXT,
  youtube_channel_id TEXT,
  categories JSONB NOT NULL DEFAULT '[]'::jsonb,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  etag TEXT,
  last_modified TEXT,
  last_checked_at TIMESTAMPTZ,
  fetch_failures INTEGER NOT NULL DEFAULT 0,
  next_fetch_after TIMESTAMPTZ
);

-- Items read from a feed. subscription_id names the feed (feeds.id), not a person.
CREATE TABLE IF NOT EXISTS feed_entries (
  id TEXT PRIMARY KEY,
  video_id BIGINT REFERENCES videos(id) ON DELETE SET NULL,
  subscription_id TEXT REFERENCES feeds(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  url TEXT,
  author TEXT,
  published_at TIMESTAMPTZ,
  raw JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Direct-RSS polling state on feeds. The CREATE TABLE above covers fresh installs;
-- these ALTERs migrate existing databases (init_db replays the whole file, and ADD
-- COLUMN IF NOT EXISTS is a no-op once the column exists).
ALTER TABLE feeds ADD COLUMN IF NOT EXISTS etag TEXT;
ALTER TABLE feeds ADD COLUMN IF NOT EXISTS last_modified TEXT;
ALTER TABLE feeds ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ;
ALTER TABLE feeds ADD COLUMN IF NOT EXISTS fetch_failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE feeds ADD COLUMN IF NOT EXISTS next_fetch_after TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_feeds_youtube_channel ON feeds (youtube_channel_id);

-- Who follows which feed. Per user_key, so per household profile too: each profile is
-- its own person. The poller fetches only feeds with at least one row here; the last
-- unsubscribe stops polling and keeps the entries and videos already stored. On the
-- upgrade that creates this table, migrations.py gives the existing feeds to every
-- user_key that has data, so nobody's feed empties.
CREATE TABLE IF NOT EXISTS user_subscriptions (
  user_key TEXT NOT NULL,
  feed_id TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE ON UPDATE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_key, feed_id)
);
CREATE INDEX IF NOT EXISTS idx_user_subscriptions_feed ON user_subscriptions (feed_id);


CREATE TABLE IF NOT EXISTS subscription_collections (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_key TEXT NOT NULL DEFAULT 'local',
  name TEXT NOT NULL,
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_key, name)
);

CREATE INDEX IF NOT EXISTS idx_subscription_collections_user_time ON subscription_collections (user_key, updated_at DESC);

CREATE TABLE IF NOT EXISTS subscription_collection_members (
  id BIGSERIAL PRIMARY KEY,
  collection_id UUID NOT NULL REFERENCES subscription_collections(id) ON DELETE CASCADE,
  subscription_id TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
  added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (collection_id, subscription_id)
);

CREATE INDEX IF NOT EXISTS idx_subscription_collection_members_collection ON subscription_collection_members (collection_id, added_at DESC);
CREATE INDEX IF NOT EXISTS idx_subscription_collection_members_subscription ON subscription_collection_members (subscription_id);

CREATE TABLE IF NOT EXISTS sync_runs (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'running',
  message TEXT,
  subscriptions_count INTEGER NOT NULL DEFAULT 0,
  entries_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS retrieval_attempts (
  id BIGSERIAL PRIMARY KEY,
  video_id BIGINT REFERENCES videos(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT,
  attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_video_events (
  id BIGSERIAL PRIMARY KEY,
  video_id BIGINT REFERENCES videos(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_videos_published_at ON videos (published_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_videos_channel_id ON videos (channel_id);
CREATE INDEX IF NOT EXISTS idx_feed_entries_published_at ON feed_entries (published_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_feed_entries_video_id ON feed_entries (video_id);
CREATE INDEX IF NOT EXISTS idx_feed_entries_video_updated ON feed_entries (video_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sync_runs_started_at ON sync_runs (started_at DESC);
ALTER TABLE user_video_events ADD COLUMN IF NOT EXISTS user_key TEXT NOT NULL DEFAULT 'local';
CREATE INDEX IF NOT EXISTS idx_user_video_events_video_id ON user_video_events (video_id);
CREATE INDEX IF NOT EXISTS idx_user_video_events_event_type ON user_video_events (event_type);
CREATE INDEX IF NOT EXISTS idx_user_video_events_user_key ON user_video_events (user_key, created_at DESC);

CREATE TABLE IF NOT EXISTS user_video_feedback (
  id BIGSERIAL PRIMARY KEY,
  user_key TEXT NOT NULL DEFAULT 'local',
  video_id BIGINT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  youtube_video_id TEXT NOT NULL,
  reaction TEXT CHECK (reaction IN ('like', 'dislike')),
  not_interested BOOLEAN NOT NULL DEFAULT false,
  not_interested_reason TEXT,
  last_event_type TEXT,
  last_shown_reason TEXT,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_key, video_id)
);

CREATE INDEX IF NOT EXISTS idx_user_video_feedback_video ON user_video_feedback (video_id);
CREATE INDEX IF NOT EXISTS idx_user_video_feedback_user_interest ON user_video_feedback (user_key, not_interested, reaction);
CREATE INDEX IF NOT EXISTS idx_user_video_feedback_youtube ON user_video_feedback (youtube_video_id);

CREATE TABLE IF NOT EXISTS playback_progress (
  id BIGSERIAL PRIMARY KEY,
  user_key TEXT NOT NULL DEFAULT 'local',
  video_id BIGINT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  youtube_video_id TEXT NOT NULL,
  first_opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  playback_started_at TIMESTAMPTZ,
  last_watched_at TIMESTAMPTZ,
  furthest_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
  current_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
  duration_seconds DOUBLE PRECISION,
  percent_completed DOUBLE PRECISION NOT NULL DEFAULT 0,
  completed BOOLEAN NOT NULL DEFAULT false,
  manually_marked_watched BOOLEAN NOT NULL DEFAULT false,
  open_count INTEGER NOT NULL DEFAULT 0,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_key, video_id)
);

CREATE INDEX IF NOT EXISTS idx_playback_progress_user_time ON playback_progress (user_key, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_playback_progress_video ON playback_progress (video_id);
CREATE INDEX IF NOT EXISTS idx_playback_progress_youtube ON playback_progress (youtube_video_id);

CREATE TABLE IF NOT EXISTS playlists (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_key TEXT NOT NULL DEFAULT 'local',
  name TEXT NOT NULL,
  description TEXT,
  visibility TEXT NOT NULL DEFAULT 'private',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_playlists_user_time ON playlists (user_key, updated_at DESC);

CREATE TABLE IF NOT EXISTS playlist_items (
  id BIGSERIAL PRIMARY KEY,
  playlist_id UUID NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  video_id BIGINT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  youtube_video_id TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (playlist_id, video_id)
);

CREATE INDEX IF NOT EXISTS idx_playlist_items_playlist_position ON playlist_items (playlist_id, position, added_at);
CREATE INDEX IF NOT EXISTS idx_playlist_items_video ON playlist_items (video_id);

-- Library tools (library_tools.py). A playlist's own sort (Manual unless the owner picks
-- another), the YouTube playlist it was imported from, and who may open it: visibility
-- above is 'private', 'unlisted' (anyone signed in to this install who has the link) or
-- 'public' (also listed on everyone's Playlists page).
ALTER TABLE playlists ADD COLUMN IF NOT EXISTS sort_key TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE playlists ADD COLUMN IF NOT EXISTS source_playlist_id TEXT;
CREATE INDEX IF NOT EXISTS idx_playlists_shared ON playlists (visibility, updated_at DESC) WHERE visibility <> 'private';

-- "Pause watch history", per profile. While paused nothing new is written to history,
-- resume positions or watch time, so watching sends the recommender no new signal.
CREATE TABLE IF NOT EXISTS user_history_settings (
  user_key TEXT PRIMARY KEY,
  paused BOOLEAN NOT NULL DEFAULT false,
  paused_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A deleted playlist or collection, kept as a snapshot so the toast's Undo can put it
-- back with the same id. Rows older than a day are purged on the next delete.
CREATE TABLE IF NOT EXISTS library_trash (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  item_id TEXT NOT NULL,
  snapshot JSONB NOT NULL,
  deleted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_library_trash_user_time ON library_trash (user_key, deleted_at DESC);

ALTER TABLE videos ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS duration_seconds INTEGER;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS view_count BIGINT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS like_count BIGINT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS dislike_count BIGINT;
-- Where a dislike count came from. YouTube stopped publishing them in 2021, so anything
-- present is either historical metadata or an estimate fetched from Return YouTube
-- Dislike; the UI has to be able to say which.
ALTER TABLE videos ADD COLUMN IF NOT EXISTS dislike_source TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS dislike_fetched_at TIMESTAMPTZ;

-- Total seconds actually watched, accumulated across every session. furthest_seconds
-- only records the deepest point reached in one sitting, so it cannot tell a video
-- watched twice from one watched once — which is exactly what ranking needs to know.
ALTER TABLE playback_progress ADD COLUMN IF NOT EXISTS total_watch_seconds DOUBLE PRECISION NOT NULL DEFAULT 0;
-- Where to pick the video back up. Deliberately separate from current_seconds, which for
-- a cross-origin player is only ever dwell time on the page: resuming at a made-up
-- position is worse than not resuming, so only a real reading from the player lands here.
ALTER TABLE playback_progress ADD COLUMN IF NOT EXISTS resume_seconds DOUBLE PRECISION;

-- Comment votes are local only. Invidious reads comments; it cannot cast a vote on
-- YouTube, so these never leave this database. They exist to keep the button state after
-- a reload and to feed the recommender a small hint about the video.
CREATE TABLE IF NOT EXISTS user_comment_feedback (
  user_key TEXT NOT NULL DEFAULT 'local',
  youtube_video_id TEXT NOT NULL,
  comment_id TEXT NOT NULL,
  reaction TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_key, comment_id)
);
CREATE INDEX IF NOT EXISTS idx_user_comment_feedback_video ON user_comment_feedback (user_key, youtube_video_id);
CREATE INDEX IF NOT EXISTS idx_playback_progress_user_watched ON playback_progress (user_key, last_watched_at DESC);
ALTER TABLE videos ADD COLUMN IF NOT EXISTS captions_status TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS metadata_source TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS metadata_fetched_at TIMESTAMPTZ;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS metadata_error TEXT;
CREATE INDEX IF NOT EXISTS idx_videos_metadata_fetched_at ON videos (metadata_fetched_at);

ALTER TABLE videos ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}'::text[];
CREATE INDEX IF NOT EXISTS idx_videos_tags ON videos USING GIN (tags);

ALTER TABLE channels ADD COLUMN IF NOT EXISTS subscriber_count BIGINT;
ALTER TABLE channels ADD COLUMN IF NOT EXISTS avatar_url TEXT;
ALTER TABLE channels ADD COLUMN IF NOT EXISTS banner_url TEXT;

ALTER TABLE videos ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'subscription';
ALTER TABLE videos ADD COLUMN IF NOT EXISTS discovery_source TEXT;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS discovered_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS video_discoveries (
  id BIGSERIAL PRIMARY KEY,
  source_video_id BIGINT REFERENCES videos(id) ON DELETE SET NULL,
  discovered_video_id BIGINT REFERENCES videos(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source_video_id, discovered_video_id, source)
);

CREATE INDEX IF NOT EXISTS idx_videos_source_kind ON videos (source_kind);
CREATE INDEX IF NOT EXISTS idx_videos_discovered_at ON videos (discovered_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_video_discoveries_discovered_video_id ON video_discoveries (discovered_video_id);


CREATE TABLE IF NOT EXISTS users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  authentik_sub TEXT UNIQUE NOT NULL,
  display_name TEXT,
  profile JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS interactions (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  video_id BIGINT REFERENCES videos(id) ON DELETE SET NULL,
  youtube_video_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  value DOUBLE PRECISION,
  session_id UUID,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS recommendation_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) ON DELETE SET NULL,
  surface TEXT NOT NULL DEFAULT 'home',
  model_version TEXT NOT NULL DEFAULT 'heuristic-v1',
  request_context JSONB NOT NULL DEFAULT '{}'::jsonb,
  candidate_count INTEGER NOT NULL DEFAULT 0,
  returned_count INTEGER NOT NULL DEFAULT 0,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS recommendation_candidates (
  id BIGSERIAL PRIMARY KEY,
  run_id UUID NOT NULL REFERENCES recommendation_runs(id) ON DELETE CASCADE,
  video_id BIGINT REFERENCES videos(id) ON DELETE SET NULL,
  youtube_video_id TEXT NOT NULL,
  lane TEXT NOT NULL,
  seed_video_id BIGINT REFERENCES videos(id) ON DELETE SET NULL,
  raw_score DOUBLE PRECISION,
  rank_score DOUBLE PRECISION,
  position INTEGER,
  reason TEXT,
  score_components JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS feed_impressions (
  id BIGSERIAL PRIMARY KEY,
  run_id UUID REFERENCES recommendation_runs(id) ON DELETE SET NULL,
  user_id UUID REFERENCES users(id) ON DELETE CASCADE,
  video_id BIGINT REFERENCES videos(id) ON DELETE SET NULL,
  youtube_video_id TEXT NOT NULL,
  surface TEXT NOT NULL DEFAULT 'home',
  lane TEXT,
  position INTEGER,
  model_version TEXT,
  shown_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS user_topic_preferences (
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  topic TEXT NOT NULL,
  preference TEXT NOT NULL,
  weight DOUBLE PRECISION NOT NULL DEFAULT 1,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, topic)
);

CREATE TABLE IF NOT EXISTS user_channel_preferences (
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  youtube_channel_id TEXT NOT NULL,
  channel_id BIGINT REFERENCES channels(id) ON DELETE SET NULL,
  preference TEXT NOT NULL,
  weight DOUBLE PRECISION NOT NULL DEFAULT 1,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, youtube_channel_id)
);

CREATE TABLE IF NOT EXISTS gorse_sync_state (
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  payload_hash TEXT,
  last_synced_at TIMESTAMPTZ,
  last_error TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (entity_type, entity_id)
);

-- Per-channel notification level: 'all', 'personalized', or 'none'. A channel with no
-- row here is treated as 'all', so subscribing keeps behaving the way it always has and
-- only an explicit choice narrows what reaches the bell.
CREATE TABLE IF NOT EXISTS channel_notifications (
  user_key TEXT NOT NULL DEFAULT 'local',
  youtube_channel_id TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'all',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_key, youtube_channel_id)
);

-- Videos kept on the server after a download. Separate from the transient files a
-- browser download produces: those are swept on a timer, these are a library and stay
-- until the user removes them.
CREATE TABLE IF NOT EXISTS downloads (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_key TEXT NOT NULL DEFAULT 'local',
  youtube_video_id TEXT NOT NULL,
  video_id BIGINT REFERENCES videos(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  filename TEXT NOT NULL,
  path TEXT NOT NULL,
  container TEXT,
  height INTEGER,
  quality TEXT,
  size_bytes BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_key, youtube_video_id)
);

CREATE INDEX IF NOT EXISTS idx_downloads_user_created ON downloads (user_key, created_at DESC);

-- Bearer tokens for programmatic access. Only a hash is stored: the plaintext is shown
-- once at creation and is unrecoverable afterwards, so a database copy cannot be replayed
-- against the API. The prefix is kept in clear purely so a lookup does not have to hash
-- every row on every request.
CREATE TABLE IF NOT EXISTS api_tokens (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_key TEXT NOT NULL DEFAULT 'local',
  name TEXT NOT NULL,
  token_prefix TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  read_only BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_api_tokens_prefix ON api_tokens (token_prefix);
CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens (user_key, created_at DESC);

-- Signal imported from a Google Takeout export. Kept in its own tables rather than
-- merged into playback_progress: those rows are keyed to videos this install actually
-- has, while a Takeout history is mostly videos it has never heard of, and an import
-- has to be reversible in one statement when somebody changes their mind.
CREATE TABLE IF NOT EXISTS takeout_channel_affinity (
  user_key TEXT NOT NULL DEFAULT 'local',
  youtube_channel_id TEXT NOT NULL,
  title TEXT,
  watch_count INTEGER NOT NULL DEFAULT 0,
  last_watched_at TIMESTAMPTZ,
  imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_key, youtube_channel_id)
);

CREATE TABLE IF NOT EXISTS takeout_watched_videos (
  user_key TEXT NOT NULL DEFAULT 'local',
  youtube_video_id TEXT NOT NULL,
  youtube_channel_id TEXT,
  watched_at TIMESTAMPTZ,
  imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_key, youtube_video_id)
);

CREATE TABLE IF NOT EXISTS takeout_imports (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_key TEXT NOT NULL DEFAULT 'local',
  summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_takeout_affinity_count ON takeout_channel_affinity (user_key, watch_count DESC);
CREATE INDEX IF NOT EXISTS idx_takeout_imports_user ON takeout_imports (user_key, created_at DESC);

-- TF-IDF vector of a video's subject, computed at enrichment time. Stored because
-- deriving it per request would mean loading every description on every page render.
ALTER TABLE videos ADD COLUMN IF NOT EXISTS topic_vector JSONB;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS topic_vector_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_videos_topic_vector_missing ON videos (id) WHERE topic_vector IS NULL;

-- Channels found in a Takeout archive's subscriptions.csv. Stored rather than acted on
-- immediately: silently subscribing to several hundred channels because somebody
-- uploaded a file is not a decision this app should make alone.
CREATE TABLE IF NOT EXISTS takeout_subscriptions (
  user_key TEXT NOT NULL DEFAULT 'local',
  youtube_channel_id TEXT NOT NULL,
  title TEXT,
  channel_url TEXT,
  imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  subscribed_at TIMESTAMPTZ,
  PRIMARY KEY (user_key, youtube_channel_id)
);

-- A channel deleted on YouTube can never be subscribed to. Recording that stops it
-- being retried on every pass and lets the UI say so instead of showing a button that
-- silently does nothing.
ALTER TABLE takeout_subscriptions ADD COLUMN IF NOT EXISTS failed_at TIMESTAMPTZ;
ALTER TABLE takeout_subscriptions ADD COLUMN IF NOT EXISTS failure_reason TEXT;

-- Poll votes cast in OpenFeed. Local only, and deliberately so: YouTube publishes no
-- per-option counts and accepts no votes without an authenticated account, so these can
-- never be sent anywhere. They exist to remember what the user picked.
CREATE TABLE IF NOT EXISTS post_poll_votes (
  user_key TEXT NOT NULL DEFAULT 'local',
  post_id TEXT NOT NULL,
  choice_index INTEGER NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_key, post_id)
);

ALTER TABLE channels ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE channels ADD COLUMN IF NOT EXISTS handle TEXT;
ALTER TABLE channels ADD COLUMN IF NOT EXISTS verified BOOLEAN;
ALTER TABLE channels ADD COLUMN IF NOT EXISTS available_tabs TEXT[];

CREATE INDEX IF NOT EXISTS idx_users_authentik_sub ON users (authentik_sub);
CREATE INDEX IF NOT EXISTS idx_interactions_user_time ON interactions (user_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_interactions_video_time ON interactions (youtube_video_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_interactions_event_type ON interactions (event_type);
CREATE INDEX IF NOT EXISTS idx_recommendation_runs_user_time ON recommendation_runs (user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_recommendation_candidates_run_position ON recommendation_candidates (run_id, position);
CREATE INDEX IF NOT EXISTS idx_recommendation_candidates_video ON recommendation_candidates (youtube_video_id);
CREATE INDEX IF NOT EXISTS idx_feed_impressions_user_time ON feed_impressions (user_id, shown_at DESC);
CREATE INDEX IF NOT EXISTS idx_feed_impressions_video_time ON feed_impressions (youtube_video_id, shown_at DESC);
-- Ranker v2 rotation reads a user's impressions by the user_key stored in metadata.
CREATE INDEX IF NOT EXISTS idx_feed_impressions_userkey_time ON feed_impressions ((metadata->>'user_key'), shown_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_channel_preferences_channel ON user_channel_preferences (youtube_channel_id);
CREATE INDEX IF NOT EXISTS idx_gorse_sync_state_status ON gorse_sync_state (status, updated_at DESC);

-- Household profiles inside one account (Settings -> Profiles). account_key is the
-- account's own identity: the Authentik sub, or 'local' with sign-in off. Each profile
-- has its own user_key, and every per-user table is keyed by user_key, so a profile is
-- separated from the others without any change to those tables. The main profile's
-- user_key IS the account_key, which is why adding profiles moves no existing data. A
-- row appears only once somebody manages profiles; until then the account is its one
-- implicit main profile. Subscriptions are per profile too (user_subscriptions).
CREATE TABLE IF NOT EXISTS account_profiles (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_key TEXT NOT NULL,
  user_key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  color TEXT NOT NULL DEFAULT 'graphite',
  is_kids BOOLEAN NOT NULL DEFAULT false,
  is_main BOOLEAN NOT NULL DEFAULT false,
  position INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_account_profiles_account ON account_profiles (account_key, position, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_account_profiles_one_main ON account_profiles (account_key) WHERE is_main;
CREATE UNIQUE INDEX IF NOT EXISTS idx_account_profiles_name ON account_profiles (account_key, lower(name));

-- Provenance: which person brought a video into the shared catalog, or touched it. Videos,
-- channels and related links are one catalog for the install (a cache of public YouTube
-- data, like Invidious'), and those rows never say who caused them. This table does, so the
-- per-person surfaces that read the catalog (recommendations, Home shelves, Shorts, Up next,
-- library search) can stay inside what that person has a reason to see. One row per
-- (person, video): `source` is the first reason (catalog_scope.SOURCES: 'search', 'watch',
-- 'event', 'feedback', 'playlist', 'channel', 'discovery', 'takeout', 'download', 'catalog'),
-- `first_seen` when.
-- See catalog_scope.py and PRIVACY.md ("Shared catalog").
CREATE TABLE IF NOT EXISTS video_provenance (
  user_key TEXT NOT NULL,
  video_id BIGINT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_key, video_id)
);
CREATE INDEX IF NOT EXISTS idx_video_provenance_video ON video_provenance (video_id);
-- The scope function reads feed entries by feed, and the classic ranker's subscription test
-- by channel; neither had an index on that side.
CREATE INDEX IF NOT EXISTS idx_feed_entries_subscription ON feed_entries (subscription_id);

-- The catalog as it stood when an install first hosted a second person (a household profile,
-- or sign-in turned on), claimed by the one person who had built it: every video id up to
-- `through_video_id`. A range instead of one provenance row per video keeps the original
-- owner's scope check a comparison (catalog_scope.claim_catalog).
CREATE TABLE IF NOT EXISTS catalog_claims (
  user_key TEXT PRIMARY KEY,
  through_video_id BIGINT NOT NULL,
  claimed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every video one person has a reason to see, beyond their claimed range: their provenance
-- rows, everything from the feeds and channels they follow, and videos discovered as related
-- to either (one link, from video_discoveries). The ranker adds the related lists stored on
-- those videos in memory (ranker.scope_mask). catalog_scope.video_filter_sql wraps it as
-- `(v.id <= <claim> OR v.id IN (SELECT openfeed_video_scope(%s)))`.
CREATE OR REPLACE FUNCTION openfeed_video_scope(p_user TEXT) RETURNS SETOF BIGINT
LANGUAGE sql STABLE ROWS 4000 AS $$
  WITH claim AS (
    SELECT COALESCE(max(cc.through_video_id), 0) AS through FROM catalog_claims cc WHERE cc.user_key = p_user
  ),
  own AS MATERIALIZED (
    SELECT vp.video_id AS id FROM video_provenance vp WHERE vp.user_key = p_user
    UNION
    SELECT v.id FROM videos v
    WHERE v.channel_id IN (
      SELECT c.id FROM channels c
      JOIN feeds f ON f.youtube_channel_id = c.youtube_channel_id
      JOIN user_subscriptions us ON us.feed_id = f.id
      WHERE us.user_key = p_user)
    UNION
    SELECT fe.video_id FROM feed_entries fe
    JOIN user_subscriptions us ON us.feed_id = fe.subscription_id
    WHERE us.user_key = p_user AND fe.video_id IS NOT NULL
  ),
  related AS MATERIALIZED (
    SELECT vd.discovered_video_id AS id FROM own JOIN video_discoveries vd ON vd.source_video_id = own.id
  )
  SELECT s.id FROM (
    SELECT id FROM own
    UNION
    SELECT id FROM related
    UNION
    SELECT vd.discovered_video_id FROM video_discoveries vd, claim
    WHERE claim.through > 0 AND vd.source_video_id <= claim.through AND vd.discovered_video_id > claim.through
  ) s
  WHERE s.id > (SELECT through FROM claim)
$$;

-- Watching, rating, saving and downloading a video are reasons to see it, whatever code
-- path wrote the row, so these are recorded by the database rather than by each caller.
CREATE OR REPLACE FUNCTION openfeed_note_provenance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.video_id IS NOT NULL AND NEW.user_key IS NOT NULL THEN
    INSERT INTO video_provenance (user_key, video_id, source)
    VALUES (NEW.user_key, NEW.video_id, TG_ARGV[0])
    ON CONFLICT (user_key, video_id) DO NOTHING;
  END IF;
  RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION openfeed_note_playlist_provenance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.video_id IS NOT NULL THEN
    INSERT INTO video_provenance (user_key, video_id, source)
    SELECT p.user_key, NEW.video_id, 'playlist' FROM playlists p WHERE p.id = NEW.playlist_id
    ON CONFLICT (user_key, video_id) DO NOTHING;
  END IF;
  RETURN NULL;
END
$$;

-- Removing a video from history (one, several or all) deletes its progress row; the
-- watching it recorded goes with it. Other reasons (a like, a playlist, a search) stay.
CREATE OR REPLACE FUNCTION openfeed_forget_watch_provenance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.video_id IS NOT NULL THEN
    DELETE FROM video_provenance
    WHERE user_key = OLD.user_key AND video_id = OLD.video_id AND source IN ('watch', 'event');
  END IF;
  RETURN NULL;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'openfeed_provenance_progress_forget') THEN
    CREATE TRIGGER openfeed_provenance_progress_forget AFTER DELETE ON playback_progress
      FOR EACH ROW EXECUTE FUNCTION openfeed_forget_watch_provenance();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'openfeed_provenance_progress') THEN
    CREATE TRIGGER openfeed_provenance_progress AFTER INSERT ON playback_progress
      FOR EACH ROW EXECUTE FUNCTION openfeed_note_provenance('watch');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'openfeed_provenance_events') THEN
    CREATE TRIGGER openfeed_provenance_events AFTER INSERT ON user_video_events
      FOR EACH ROW EXECUTE FUNCTION openfeed_note_provenance('event');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'openfeed_provenance_feedback') THEN
    CREATE TRIGGER openfeed_provenance_feedback AFTER INSERT ON user_video_feedback
      FOR EACH ROW EXECUTE FUNCTION openfeed_note_provenance('feedback');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'openfeed_provenance_downloads') THEN
    CREATE TRIGGER openfeed_provenance_downloads AFTER INSERT ON downloads
      FOR EACH ROW EXECUTE FUNCTION openfeed_note_provenance('download');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'openfeed_provenance_playlist_items') THEN
    CREATE TRIGGER openfeed_provenance_playlist_items AFTER INSERT ON playlist_items
      FOR EACH ROW EXECUTE FUNCTION openfeed_note_playlist_provenance();
  END IF;
END
$$;
