create table experiments (
    id text primary key,
    salt text not null,
    traffic int not null check (traffic between 0 and 100),
    status text not null default 'draft'
        check (status in ('draft', 'running', 'paused', 'stopped')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table variants (
    experiment_id text not null references experiments (id),
    key text not null,
    -- Bucket ranges follow this order, so it must survive a round trip through the DB.
    position int not null,
    weight int not null check (weight > 0),
    payload jsonb not null default '{}',
    source text not null default 'static' check (source in ('static', 'llm')),
    approved bool not null default true,
    unique (experiment_id, key),
    unique (experiment_id, position)
);

create table exposures (
    experiment_id text not null references experiments (id),
    visitor_id text not null,
    variant_key text not null,
    created_at timestamptz not null default now(),
    unique (experiment_id, visitor_id)
);

create table conversions (
    experiment_id text not null references experiments (id),
    visitor_id text not null,
    variant_key text not null,
    goal text not null,
    created_at timestamptz not null default now(),
    unique (experiment_id, visitor_id, goal)
);

-- Supabase exposes the public schema through its Data API. With RLS on and no policies,
-- that API sees nothing, while this service connects as the table owner and is unaffected.
alter table experiments enable row level security;
alter table variants enable row level security;
alter table exposures enable row level security;
alter table conversions enable row level security;
