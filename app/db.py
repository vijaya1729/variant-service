import os

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.models import Experiment, Variant

_SELECT_EXPERIMENTS = """
    select e.id, e.salt, e.traffic, e.status, v.key, v.weight, v.payload
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
            Variant(key=row["key"], weight=row["weight"], payload=row["payload"])
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
                insert into variants (experiment_id, key, position, weight, payload)
                values (%s, %s, %s, %s, %s)
                """,
                [
                    (experiment.id, variant.key, position, variant.weight, Jsonb(variant.payload))
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
    """Returns False if the experiment was no longer in from_status."""
    with connect() as conn:
        cur = conn.execute(
            "update experiments set status = %s, updated_at = now() where id = %s and status = %s",
            (to_status, experiment_id, from_status),
        )
        return cur.rowcount == 1


def record_exposure(experiment_id: str, visitor_id: str, variant_key: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            insert into exposures (experiment_id, visitor_id, variant_key)
            values (%s, %s, %s)
            on conflict do nothing
            """,
            (experiment_id, visitor_id, variant_key),
        )


def record_conversion(experiment_id: str, visitor_id: str, variant_key: str, goal: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            insert into conversions (experiment_id, visitor_id, variant_key, goal)
            values (%s, %s, %s, %s)
            on conflict do nothing
            """,
            (experiment_id, visitor_id, variant_key, goal),
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
