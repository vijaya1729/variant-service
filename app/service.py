import logging

from psycopg.errors import UniqueViolation

from app import db
from app.assignment import assign
from app.config_cache import cache
from app.models import Experiment, ExperimentCreate, ExperimentStatus

logger = logging.getLogger(__name__)

TRANSITIONS: dict[str, set[str]] = {
    "draft": {"running"},
    "running": {"paused", "stopped"},
    "paused": {"running", "stopped"},
    "stopped": set(),
}


class ExperimentExists(Exception):
    pass


class ExperimentNotFound(Exception):
    pass


class InvalidTransition(Exception):
    pass


def create_experiment(experiment: ExperimentCreate) -> Experiment:
    try:
        db.create_experiment(experiment)
    except UniqueViolation as exc:
        raise ExperimentExists(experiment.id) from exc
    return experiment


def get_experiment(experiment_id: str) -> Experiment:
    experiment = db.get_experiment(experiment_id)
    if experiment is None:
        raise ExperimentNotFound(experiment_id)
    return experiment


def change_status(experiment_id: str, status: ExperimentStatus) -> Experiment:
    experiment = get_experiment(experiment_id)
    if status not in TRANSITIONS[experiment.status]:
        raise InvalidTransition(f"cannot go from {experiment.status} to {status}")
    if not db.update_status(experiment_id, experiment.status, status):
        raise InvalidTransition(f"{experiment_id} changed status concurrently")
    return experiment.model_copy(update={"status": status})


def assign_variants(visitor_id: str, experiment_ids: list[str]) -> dict[str, dict | None]:
    try:
        running = cache.get()
        return {
            experiment_id: _assignment(visitor_id, running.get(experiment_id))
            for experiment_id in experiment_ids
        }
    except Exception:
        # The customer's page must render whatever happens here, and null means control.
        logger.exception("assignment failed, serving control")
        return dict.fromkeys(experiment_ids)


def _assignment(visitor_id: str, experiment: Experiment | None) -> dict | None:
    if experiment is None:
        return None
    variant = assign(visitor_id, experiment)
    if variant is None:
        return None
    return {"variant": variant.key, "payload": variant.payload}
