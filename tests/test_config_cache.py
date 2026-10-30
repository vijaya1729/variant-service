import logging

import pytest

from app.config_cache import ConfigCache
from app.models import Experiment, Variant

TTL = 30


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Loader:
    def __init__(self, result: list[Experiment] | Exception) -> None:
        self.result = result
        self.calls = 0

    def __call__(self) -> list[Experiment]:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_experiment(experiment_id: str = "exp") -> Experiment:
    variants = [Variant(key="A", weight=50), Variant(key="B", weight=50)]
    return Experiment(id=experiment_id, salt="s", traffic=100, variants=variants, status="running")


def test_loads_on_first_get_and_reuses_within_ttl() -> None:
    clock, loader = Clock(), Loader([make_experiment()])
    cache = ConfigCache(loader, TTL, clock)

    assert list(cache.get()) == ["exp"]
    clock.now += TTL - 1
    cache.get()

    assert loader.calls == 1


def test_reloads_after_ttl() -> None:
    clock, loader = Clock(), Loader([make_experiment("old")])
    cache = ConfigCache(loader, TTL, clock)
    cache.get()

    loader.result = [make_experiment("new")]
    clock.now += TTL

    assert list(cache.get()) == ["new"]


def test_successful_empty_load_replaces_config() -> None:
    clock, loader = Clock(), Loader([make_experiment()])
    cache = ConfigCache(loader, TTL, clock)
    cache.get()

    loader.result = []
    clock.now += TTL

    assert cache.get() == {}


def test_serves_stale_config_and_warns_when_loader_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock, loader = Clock(), Loader([make_experiment()])
    cache = ConfigCache(loader, TTL, clock)
    cache.get()

    loader.result = RuntimeError("db down")
    clock.now += TTL
    with caplog.at_level(logging.WARNING):
        assert list(cache.get()) == ["exp"]

    assert "db down" in caplog.text


def test_empty_when_there_has_never_been_a_successful_load() -> None:
    cache = ConfigCache(Loader(RuntimeError("db down")), TTL, Clock())
    assert cache.get() == {}


def test_failed_refresh_is_not_retried_until_the_next_ttl() -> None:
    clock, loader = Clock(), Loader(RuntimeError("db down"))
    cache = ConfigCache(loader, TTL, clock)

    cache.get()
    cache.get()
    assert loader.calls == 1

    clock.now += TTL
    cache.get()
    assert loader.calls == 2


def test_recovers_after_a_failed_refresh() -> None:
    clock, loader = Clock(), Loader(RuntimeError("db down"))
    cache = ConfigCache(loader, TTL, clock)
    cache.get()

    loader.result = [make_experiment()]
    clock.now += TTL

    assert list(cache.get()) == ["exp"]
