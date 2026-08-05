from __future__ import annotations

import os
import sys

import psycopg

from bot.people_seeds import seed_and_backfill_egor
from bot.projects import seed_projects


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

CREATE TABLE IF NOT EXISTS projects (
    id bigserial PRIMARY KEY,
    code text NOT NULL UNIQUE,
    name text NOT NULL,
    emoji text,
    is_active boolean NOT NULL DEFAULT true,
    sort_order integer NOT NULL DEFAULT 100,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS videos (
    id bigserial PRIMARY KEY,
    status text NOT NULL DEFAULT 'draft',
    video_type text NOT NULL DEFAULT 'regular',
    project_id bigint NULL REFERENCES projects(id),
    project_code text,
    project_name text,
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
    sheet_sync_status text NOT NULL DEFAULT 'not_queued',
    sheet_sync_attempts integer NOT NULL DEFAULT 0,
    sheet_sync_error text,
    sheet_synced_at timestamptz,
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

CREATE TABLE IF NOT EXISTS admin_queue_state (
    queue_name text PRIMARY KEY,
    active_video_id bigint REFERENCES videos(id) ON DELETE SET NULL,
    active_chat_id bigint,
    active_message_id bigint,
    claimed_by_tg_id bigint,
    claimed_by_username text,
    claimed_at timestamptz,
    dashboard_chat_id bigint,
    dashboard_message_id bigint,
    dashboard_updated_at timestamptz,
    queue_filter_type text NOT NULL DEFAULT 'global',
    queue_filter_value text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS daily_reports (
    report_date date PRIMARY KEY,
    telegram_chat_id bigint,
    telegram_message_id bigint,
    sent_at timestamptz NOT NULL DEFAULT now(),
    payload jsonb
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

CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id bigint PRIMARY KEY,
    update_type text,
    tg_user_id bigint,
    chat_id bigint,
    status text NOT NULL DEFAULT 'processing',
    attempts integer NOT NULL DEFAULT 1,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    processing_started_at timestamptz,
    finished_at timestamptz,
    last_error text,
    payload_hash text
);

CREATE TABLE IF NOT EXISTS background_jobs (
    id bigserial PRIMARY KEY,
    kind text NOT NULL,
    dedupe_key text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'queued',
    priority integer NOT NULL DEFAULT 100,
    attempts integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 8,
    available_at timestamptz NOT NULL DEFAULT now(),
    locked_at timestamptz,
    locked_by text,
    started_at timestamptz,
    finished_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bulk_operations (
    id bigserial PRIMARY KEY,
    kind text NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    total_count integer NOT NULL DEFAULT 0,
    processed_count integer NOT NULL DEFAULT 0,
    success_count integer NOT NULL DEFAULT 0,
    failure_count integer NOT NULL DEFAULT 0,
    last_video_id bigint,
    created_by_tg_id bigint,
    created_by_username text,
    started_at timestamptz,
    finished_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_name text PRIMARY KEY,
    last_started_at timestamptz,
    last_finished_at timestamptz,
    last_success_at timestamptz,
    last_error_at timestamptz,
    last_error text,
    last_claimed integer NOT NULL DEFAULT 0,
    last_done integer NOT NULL DEFAULT 0,
    last_remaining integer NOT NULL DEFAULT 0,
    source text,
    invocation_id text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS schema_versions (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_videos_instagram_id ON videos(instagram_id);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_publish_date ON videos(publish_date);
CREATE INDEX IF NOT EXISTS idx_videos_batch_id ON videos(batch_id);
CREATE INDEX IF NOT EXISTS idx_videos_pending_fifo ON videos(status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_people_role_active ON people(role, is_active);
CREATE INDEX IF NOT EXISTS idx_logs_entity ON logs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_metrics_video_platform_time
ON video_metrics_snapshots(video_id, platform, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_platform_time
ON video_metrics_snapshots(platform, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_sessions_updated_at ON user_sessions(updated_at);
CREATE INDEX IF NOT EXISTS idx_telegram_updates_status ON telegram_updates(status, processing_started_at);
CREATE INDEX IF NOT EXISTS idx_background_jobs_ready
ON background_jobs(status, available_at, priority, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_background_jobs_active_dedupe
ON background_jobs(dedupe_key)
WHERE dedupe_key IS NOT NULL
  AND status IN ('queued', 'processing');
CREATE INDEX IF NOT EXISTS idx_bulk_operations_status ON bulk_operations(status, created_at);

ALTER TABLE videos ADD COLUMN IF NOT EXISTS publish_date_set_by_tg_id bigint NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS video_type text NOT NULL DEFAULT 'regular';
ALTER TABLE videos ADD COLUMN IF NOT EXISTS publish_date_set_by_username text NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS publish_date_set_at timestamptz NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS montage_same_as_author boolean DEFAULT false;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS admin_message_chat_id bigint NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS admin_message_id bigint NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS admin_notified_at timestamptz NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS author_username text NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS montage_username text NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS voice_username text NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS youtube_id text NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS youtube_views bigint NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS youtube_likes bigint NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS youtube_comments bigint NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS youtube_last_sync_at timestamptz NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS project_id bigint NULL REFERENCES projects(id);
ALTER TABLE videos ADD COLUMN IF NOT EXISTS project_code text NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS project_name text NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS sheet_sync_status text NOT NULL DEFAULT 'not_queued';
ALTER TABLE videos ADD COLUMN IF NOT EXISTS sheet_sync_attempts integer NOT NULL DEFAULT 0;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS sheet_sync_error text NULL;
ALTER TABLE videos ADD COLUMN IF NOT EXISTS sheet_synced_at timestamptz NULL;

ALTER TABLE admin_queue_state ADD COLUMN IF NOT EXISTS dashboard_chat_id bigint NULL;
ALTER TABLE admin_queue_state ADD COLUMN IF NOT EXISTS dashboard_message_id bigint NULL;
ALTER TABLE admin_queue_state ADD COLUMN IF NOT EXISTS dashboard_updated_at timestamptz NULL;
ALTER TABLE admin_queue_state ADD COLUMN IF NOT EXISTS queue_filter_type text NOT NULL DEFAULT 'global';
ALTER TABLE admin_queue_state ADD COLUMN IF NOT EXISTS queue_filter_value text NULL;

UPDATE admin_queue_state
SET queue_filter_type = 'global', queue_filter_value = NULL
WHERE queue_filter_type NOT IN ('global', 'project', 'other', 'unassigned');

UPDATE videos
SET video_type = 'regular'
WHERE video_type IS NULL OR video_type NOT IN ('regular', 'bigrecap');

ALTER TABLE videos ALTER COLUMN video_type SET DEFAULT 'regular';
ALTER TABLE videos ALTER COLUMN video_type SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_videos_video_type ON videos(video_type);
CREATE INDEX IF NOT EXISTS idx_videos_youtube_id ON videos(youtube_id);
CREATE INDEX IF NOT EXISTS idx_videos_project_id ON videos(project_id);
CREATE INDEX IF NOT EXISTS idx_videos_status_project ON videos(status, project_id);
CREATE INDEX IF NOT EXISTS idx_videos_tiktok_id ON videos(tiktok_id);
CREATE INDEX IF NOT EXISTS idx_videos_vk_id ON videos(vk_id);
CREATE INDEX IF NOT EXISTS idx_people_username_lower ON people(lower(username));
CREATE INDEX IF NOT EXISTS idx_people_name_lower ON people(lower(name));

INSERT INTO admin_queue_state (queue_name)
VALUES ('main')
ON CONFLICT (queue_name) DO NOTHING;

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

DROP TRIGGER IF EXISTS trg_projects_updated_at ON projects;
CREATE TRIGGER trg_projects_updated_at
BEFORE UPDATE ON projects
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_background_jobs_updated_at ON background_jobs;
CREATE TRIGGER trg_background_jobs_updated_at
BEFORE UPDATE ON background_jobs
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_bulk_operations_updated_at ON bulk_operations;
CREATE TRIGGER trg_bulk_operations_updated_at
BEFORE UPDATE ON bulk_operations
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

INSERT INTO schema_versions (version)
VALUES ('1.0.15')
ON CONFLICT (version) DO NOTHING;

INSERT INTO schema_versions (version)
VALUES ('1.0.16')
ON CONFLICT (version) DO NOTHING;
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
        seed_projects(conn)
        seed_and_backfill_egor(conn)
        conn.commit()
    print("Database schema is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
