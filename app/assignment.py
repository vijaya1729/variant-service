import hashlib

from app.models import Experiment, Variant

BUCKETS = 10000


def bucket(*parts: str) -> int:
    # sha256, not hash(): built-in str hashing is salted per process, so buckets
    # would differ between instances and restarts.
    digest = hashlib.sha256(":".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") % BUCKETS


def variant_for_bucket(value: int, variants: list[Variant]) -> Variant:
    upper = 0
    for variant in variants:
        upper += variant.weight * BUCKETS // 100
        if value < upper:
            return variant
    raise ValueError(f"bucket {value} is outside the variant ranges")


def assign(visitor_id: str, experiment: Experiment) -> Variant | None:
    if experiment.status != "running":
        return None

    # Enrolment and variant choice hash separately so that changing traffic
    # doesn't reshuffle the variants of visitors who are already enrolled.
    if bucket(experiment.salt, "enroll", visitor_id) >= experiment.traffic * BUCKETS // 100:
        return None

    return variant_for_bucket(bucket(experiment.salt, "variant", visitor_id), experiment.variants)
