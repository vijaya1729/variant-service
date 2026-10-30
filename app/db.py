import asyncio
import os

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.models import Experiment, Variant

TRACKING_CONNECT_TIMEOUT_SECONDS = 1.0

_SELECT_EXPERIMENTS = """
    select e.id, e.salt, e.traffic, e.status, v.key, v.weight, v.payload, v.source, v.approved
    from experiments e
    join variants v on v.experiment_id = e.id
"""


def connect() -> psycopg.Connection:
    # prepare_threshold=None: Supabase's transaction pooler doesn't support prepared statements
    return psycopg.connect(os.environ["DATABASE_URL"], prepare_threshold=None, connect_timeout=3)


def _to_experiments(rows: list[dict]) -> list[Experiment]:
    experiments: dict[str, dict] = {}
    for row in rows:
        experiment = experiments.setdefault(
            row["id"],
            {
                "id": row["id"],
                "salt": row["salt"],
                "traffic": row["traffic"],
                "status": row["status"],
                "variants": [],
            },
        )
        experiment["variants"].append(
            Variant(
                key=row["key"],
                weight=row["weight"],
                payload=row["payload"],
                source=row["source"],
                approved=row["approved"],
            )
        )
    return [Experiment(**experiment) for experiment in experiments.values()]


def create_experiment(experiment: Experiment) -> None:
    with connect() as conn:
        conn.execute(
            "insert into experiments (id, salt, traffic, status) values (%s, %s, %s, %s)",
            (experiment.id, experiment.salt, experiment.traffic, experiment.status),
        )
        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into variants
                    (experiment_id, key, position, weight, payload, source, approved)
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        experiment.id,
                        variant.key,
                        position,
                        variant.weight,
                        Jsonb(variant.payload),
                        variant.source,
                        variant.approved,
                    )
                    for position, variant in enumerate(experiment.variants)
                ],
            )


def get_experiment(experiment_id: str) -> Experiment | None:
    with connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_EXPERIMENTS + " where e.id = %s order by v.position", (experiment_id,))
        experiments = _to_experiments(cur.fetchall())
    return experiments[0] if experiments else None


def list_running_experiments() -> list[Experiment]:
    with connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_EXPERIMENTS + " where e.status = 'running' order by e.id, v.position")
        return _to_experiments(cur.fetchall())


def update_status(experiment_id: str, from_status: str, to_status: str) -> bool:
    """Returns False if the experiment left from_status or can't go live yet."""
    with connect() as conn:
        if not _lock_experiment(conn, experiment_id, from_status):
            return False
        if to_status == "running" and _has_unapproved_variant(conn, experiment_id):
            return False
        conn.execute(
            "update experiments set status = %s, updated_at = now() where id = %s",
            (to_status, experiment_id),
        )
        return True


def _lock_experiment(conn: psycopg.Connection, experiment_id: str, status: str) -> bool:
    # Status changes and generated copy both take this row lock, so copy can't be saved
    # unapproved between an approval check and the experiment going live.
    cur = conn.execute(
        "select 1 from experiments where id = %s and status = %s for update",
        (experiment_id, status),
    )
    return cur.fetchone() is not None


def _has_unapproved_variant(conn: psycopg.Connection, experiment_id: str) -> bool:
    cur = conn.execute(
        "select 1 from variants where experiment_id = %s and not approved limit 1",
        (experiment_id,),
    )
    return cur.fetchone() is not None


def save_generated_copy(
    experiment_id: str, variant_key: str, field: str, text: str, meta: dict
) -> bool:
    """Returns False if the experiment stopped being a draft during generation."""
    with connect() as conn:
        if not _lock_experiment(conn, experiment_id, "draft"):
            return False
        cur = conn.execute(
            """
            update variants
            set payload = payload || %s, source = 'llm', approved = false, generation_meta = %s
            where experiment_id = %s and key = %s
            """,
            (Jsonb({field: text}), Jsonb(meta), experiment_id, variant_key),
        )
        return cur.rowcount == 1


def approve_variant(experiment_id: str, variant_key: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "update variants set approved = true where experiment_id = %s and key = %s",
            (experiment_id, variant_key),
        )
        return cur.rowcount == 1


async def _tracking_insert(sql: str, params: tuple) -> None:
    # wait_for because psycopg raises any connect_timeout below 2s to 2s.
    conn = await asyncio.wait_for(
        psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"], prepare_threshold=None),
        TRACKING_CONNECT_TIMEOUT_SECONDS,
    )
    async with conn:
        # SET LOCAL: a plain SET would outlive this transaction on the pooler's shared
        # server connection and apply to whichever client gets that connection next.
        await conn.execute("set local statement_timeout = '500ms'")
        await conn.execute(sql, params)


def record_exposure(experiment_id: str, visitor_id: str, variant_key: str) -> None:
    asyncio.run(
        _tracking_insert(
            """
            insert into exposures (experiment_id, visitor_id, variant_key)
            values (%s, %s, %s)
            on conflict do nothing
            """,
            (experiment_id, visitor_id, variant_key),
        )
    )


def record_conversion(experiment_id: str, visitor_id: str, variant_key: str, goal: str) -> None:
    asyncio.run(
        _tracking_insert(
            """
            insert into conversions (experiment_id, visitor_id, variant_key, goal)
            values (%s, %s, %s, %s)
            on conflict do nothing
            """,
            (experiment_id, visitor_id, variant_key, goal),
        )
    )


def get_variant_counts(experiment_id: str, goal: str) -> list[dict]:
    """One row per variant in position order, or no rows for an unknown experiment."""
    # Starts from variants so a variant with no events still appears. The goal filter
    # belongs in the join: in a where clause it would drop exposed non-converters.
    with connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select
                v.key,
                v.weight,
                count(x.visitor_id) as exposures,
                count(c.visitor_id) as conversions
            from variants v
            left join exposures x
                on x.experiment_id = v.experiment_id and x.variant_key = v.key
            left join conversions c
                on c.experiment_id = x.experiment_id
                and c.visitor_id = x.visitor_id
                and c.goal = %s
            where v.experiment_id = %s
            group by v.key, v.weight, v.position
            order by v.position
            """,
            (goal, experiment_id),
        )
        return cur.fetchall()
