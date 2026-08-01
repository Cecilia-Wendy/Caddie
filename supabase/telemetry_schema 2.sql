-- Caddie Alpha anonymous product telemetry.
-- Safe for Supabase SQL Editor. Clients can insert approved coarse events,
-- but cannot read, update, or delete any analytics rows.

create table if not exists public.telemetry_events (
    event_id uuid primary key,
    event_name text not null check (
        event_name in (
            'analytics_consent_updated',
            'onboarding_started',
            'onboarding_completed',
            'view_opened',
            'source_added',
            'source_ingested',
            'job_track_created',
            'asset_generated',
            'asset_saved',
            'feedback_submitted',
            'proposed_change_accepted',
            'proposed_change_rejected'
        )
    ),
    schema_version integer not null default 1 check (schema_version = 1),
    installation_id uuid not null,
    session_id uuid,
    entity_type text check (
        entity_type is null or entity_type in (
            'source',
            'job_track',
            'asset',
            'product_feedback',
            'proposed_change'
        )
    ),
    entity_id_hash text check (
        entity_id_hash is null or entity_id_hash ~ '^[0-9a-f]{64}$'
    ),
    properties jsonb not null default '{}'::jsonb check (
        jsonb_typeof(properties) = 'object'
        and octet_length(properties::text) <= 2048
    ),
    client_time timestamptz not null,
    server_time timestamptz not null default now(),
    app_version text not null check (
        app_version ~ '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'
    ),
    platform text not null default 'macos' check (platform = 'macos')
);

create index if not exists idx_telemetry_events_name_time
    on public.telemetry_events(event_name, server_time);

create index if not exists idx_telemetry_events_installation_time
    on public.telemetry_events(installation_id, server_time);

alter table public.telemetry_events enable row level security;

revoke all on table public.telemetry_events from anon, authenticated;
grant insert on table public.telemetry_events to anon, authenticated;

drop policy if exists "anonymous telemetry insert only"
    on public.telemetry_events;

create policy "anonymous telemetry insert only"
    on public.telemetry_events
    for insert
    to anon, authenticated
    with check (
        schema_version = 1
        and platform = 'macos'
        and client_time >= now() - interval '30 days'
        and client_time <= now() + interval '1 day'
    );

comment on table public.telemetry_events is
    'Anonymous coarse Caddie product events. No career text, prompts, paths, names, emails, phone numbers, or API keys.';
