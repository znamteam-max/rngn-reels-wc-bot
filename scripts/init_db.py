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
    montage_id bigint NULL REFERENCES people(id),
    montage_name text,
    voice_id bigint NULL REFERENCES people(id),
    voice_name text,
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
CREATE INDEX IF NOT EXISTS idx_user_sessions_updated_at ON user_sessions(updated_at);

ALTER TABLE videos ADD COLUMN IF NOT EXISTS publish_date_set_by_tg_id bigint NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS publish_date_set_by_username text NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS publish_date_set_at timestamptz NULL;

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
