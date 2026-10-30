import logging
from collections.abc import Iterator
from contextlib import contextmanager

from psycopg.errors import UniqueViolation
from pydantic import ValidationError

from app import db
from app.assignment import assign
from app.config_cache import cache
from app.models import (
    ConversionEvent,
    Experiment,
    ExperimentCreate,
    ExperimentStatus,
    ExposureEvent,
)

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


@contextmanager
def _dropping_failures() -> Iterator[None]:
    # Trade-off: tracking must never hold up or break the customer's page, so a malformed
    # event or a DB error loses that event, and the client still gets a 202. Results can
    # undercount during an outage; I prefer that to making the page wait on us.
    try:
        yield
    except ValidationError:
        logger.info("ignoring malformed tracking event")
    except Exception:
        logger.exception("dropping tracking event")


def _variant_key(experiment_id: str, visitor_id: str) -> str | None:
    # Recomputed rather than taken from the client, so a variant can't be spoofed and
    # unknown, non-running or non-enrolled events have nothing to record.
    experiment = cache.get().get(experiment_id)
    if experiment is None:
        return None
    variant = assign(visitor_id, experiment)
    return variant.key if variant else None


def track_exposure(raw_event: bytes) -> None:
    with _dropping_failures():
        event = ExposureEvent.model_validate_json(raw_event)
        variant_key = _variant_key(event.experiment_id, event.visitor_id)
        if variant_key:
            db.record_exposure(event.experiment_id, event.visitor_id, variant_key)


def track_conversion(raw_event: bytes) -> None:
    with _dropping_failures():
        event = ConversionEvent.model_validate_json(raw_event)
        variant_key = _variant_key(event.experiment_id, event.visitor_id)
        if variant_key:
            db.record_conversion(event.experiment_id, event.visitor_id, variant_key, event.goal)
