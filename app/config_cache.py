import logging
import os
import threading
import time
from collections.abc import Callable

from app import db
from app.models import Experiment

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 30.0


class ConfigCache:
    def __init__(
        self,
        loader: Callable[[], list[Experiment]],
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loader = loader
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._experiments: dict[str, Experiment] = {}
        self._last_attempt: float | None = None
        self._lock = threading.Lock()

    def get(self) -> dict[str, Experiment]:
        # Only one request refreshes at a time; the rest serve what's already here.
        if self._is_due() and self._lock.acquire(blocking=False):
            try:
                self._refresh()
            finally:
                self._lock.release()
        return self._experiments

    def _is_due(self) -> bool:
        if self._last_attempt is None:
            return True
        return self._clock() - self._last_attempt >= self._ttl_seconds

    def _refresh(self) -> None:
        try:
            self._experiments = {experiment.id: experiment for experiment in self._loader()}
        except Exception as exc:
            logger.warning("config refresh failed, keeping last-known config: %s", exc)
        # Failures count as attempts too. Otherwise, while the DB is down, every request
        # would wait out the connect timeout on the page-render path.
        self._last_attempt = self._clock()


def _ttl_from_env() -> float:
    value = os.environ.get("CONFIG_TTL_SECONDS", DEFAULT_TTL_SECONDS)
    try:
        return float(value)
    except ValueError:
        logger.warning("invalid CONFIG_TTL_SECONDS %r, using %s", value, DEFAULT_TTL_SECONDS)
        return DEFAULT_TTL_SECONDS


cache = ConfigCache(db.list_running_experiments, _ttl_from_env())
