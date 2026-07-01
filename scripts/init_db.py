from __future__ import annotations

import os
import sys

import psycopg


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS people (
    id bigserial PRIMARY KEY,
    name text NOT NULL,
    tg_id bigint NULL,
    username text NULL,
    role text NOT NULL CHECK (role IN ('author','montage','voice','admin','superadmin')),
    is_active boolean DEFAULT true,
    sort_weight int DEFAULT 0,
    created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS batches (
    id bigserial PRIMARY KEY,
    status text NOT NULL DEFAULT 'open',
    created_by_tg_id bigint,
    created_by_username text,
    total_count int DEFAULT 0,
    clean_count int DEFAULT 0,
    duplicate_count int DEFAULT 0,
    problem_count int DEFAULT 0,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS videos (
    id bigserial PRIMARY KEY,
    status text NOT NULL DEFAULT 'draft',
    publish_date date,
    instagram_url text,
    instagram_id text UNIQUE,
    youtube_url text,
    youtube_id text,
    tiktok_url text,
    tiktok_id text,
    vk_url text,
    vk_id text,
    author_id bigint NULL REFERENCES people(id),
    author_name text,
    author_username text NULL,
    montage_id bigint NULL REFERENCES people(id),
    montage_name text,
    montage_username text NULL,
    montage_same_as_author boolean DEFAULT false,
    voice_id bigint NULL REFERENCES people(id),
    voice_name text,
    voice_username text NULL,
    added_by_tg_id bigint,
    added_by_username text,
    checked_by_tg_id bigint,
    checked_by_username text,
    publish_date_set_by_tg_id bigint NULL,
    publish_date_set_by_username text NULL,
    publish_date_set_at timestamptz NULL,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    checked_at timestamptz,
    batch_id bigint NULL REFERENCES batches(id),
    sheet_row int NULL,
    admin_message_chat_id bigint NULL,
    admin_message_id bigint NULL,
    admin_notified_at timestamptz NULL,
    comment text
);

CREATE TABLE IF NOT EXISTS admin_locks (
    video_id bigint PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
    admin_tg_id bigint,
    locked_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS logs (
    id bigserial PRIMARY KEY,
    entity_type text,
    entity_id bigint,
    action text,
    actor_tg_id bigint,
    actor_username text,
    before_data jsonb,
    after_data jsonb,
    created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS video_metrics_snapshots (
    id bigserial PRIMARY KEY,
    video_id bigint NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    platform text NOT NULL,
    platform_video_id text,
    platform_url text,
    captured_at timestamptz NOT NULL DEFAULT now(),
    views bigint,
    likes bigint,
    comments bigint,
    shares bigint,
    source_status text NOT NULL DEFAULT 'ok',
    error_message text,
    raw_data jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_sessions (
    tg_id bigint PRIMARY KEY,
    chat_id bigint NOT NULL,
    username text,
    state text NOT NULL,
    data jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_videos_instagram_id ON videos(instagram_id);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_publish_date ON videos(publish_date);
CREATE INDEX IF NOT EXISTS idx_videos_batch_id ON videos(batch_id);
CREATE INDEX IF NOT EXISTS idx_people_role_active ON people(role, is_active);
CREATE INDEX IF NOT EXISTS idx_logs_entity ON logs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_metrics_video_platform_time
ON video_metrics_snapshots(video_id, platform, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_platform_time
ON video_metrics_snapshots(platform, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_sessions_updated_at ON user_sessions(updated_at);

ALTER TABLE videos ADD COLUMN IF NOT EXISTS publish_date_set_by_tg_id bigint NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS publish_date_set_by_username text NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS publish_date_set_at timestamptz NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS montage_same_as_author boolean DEFAULT false;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS admin_message_chat_id bigint NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS admin_message_id bigint NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS admin_notified_at timestamptz NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS author_username text NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS montage_username text NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS voice_username text NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS youtube_views bigint NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS youtube_likes bigint NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS youtube_comments bigint NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS youtube_last_sync_at timestamptz NULL;

UPDATE videos v
SET author_username = p.username
FROM people p
WHERE v.author_id = p.id
  AND v.author_username IS NULL;

UPDATE videos v
SET montage_username = p.username
FROM people p
WHERE v.montage_id = p.id
  AND v.montage_username IS NULL;

UPDATE videos v
SET voice_username = p.username
FROM people p
WHERE v.voice_id = p.id
  AND v.voice_username IS NULL;

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_videos_updated_at ON videos;
CREATE TRIGGER trg_videos_updated_at
BEFORE UPDATE ON videos
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_batches_updated_at ON batches;
CREATE TRIGGER trg_batches_updated_at
BEFORE UPDATE ON batches
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_user_sessions_updated_at ON user_sessions;
CREATE TRIGGER trg_user_sessions_updated_at
BEFORE UPDATE ON user_sessions
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();
"""


def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not configured.", file=sys.stderr)
        return 1

    print("Initializing database schema...")
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()
    print("Database schema is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
