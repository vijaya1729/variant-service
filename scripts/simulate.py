"""Send simulated visitors through the live API.

python scripts/simulate.py --base-url https://example.vercel.app \
    --experiment cta-test --rates A=0.05,B=0.07
"""

import argparse
import asyncio
import math
import random
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field

import httpx

ASSIGN_BUDGET_MS = 200
REQUEST_TIMEOUT_SECONDS = 10


@dataclass
class Tally:
    assigned: Counter = field(default_factory=Counter)
    converted: Counter = field(default_factory=Counter)
    latencies_ms: list[float] = field(default_factory=list)
    not_assigned: int = 0
    failed_requests: int = 0


def parse_rates(value: str) -> dict[str, float]:
    rates = {}
    for pair in value.split(","):
        key, _, rate = pair.partition("=")
        rates[key.strip()] = float(rate)
        if not 0 <= rates[key.strip()] <= 1:
            raise argparse.ArgumentTypeError(f"rate for {key} must be between 0 and 1")
    return rates


def percentile(sorted_values: list[float], percent: float) -> float:
    rank = math.ceil(percent / 100 * len(sorted_values))
    return sorted_values[max(rank, 1) - 1]


async def track(client: httpx.AsyncClient, path: str, body: dict, tally: Tally) -> None:
    try:
        response = await client.post(path, json=body)
        if response.status_code != 202:
            tally.failed_requests += 1
    except httpx.HTTPError:
        tally.failed_requests += 1


async def simulate_visitor(
    client: httpx.AsyncClient,
    experiment: str,
    rates: dict[str, float],
    rng: random.Random,
    limit: asyncio.Semaphore,
    tally: Tally,
) -> None:
    visitor_id = str(uuid.UUID(int=rng.getrandbits(128)))
    body = {"experiment_id": experiment, "visitor_id": visitor_id}

    async with limit:
        started = time.perf_counter()
        try:
            response = await client.post(
                "/assign", json={"visitor_id": visitor_id, "experiment_ids": [experiment]}
            )
        except httpx.HTTPError:
            tally.failed_requests += 1
            return
        tally.latencies_ms.append((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            tally.failed_requests += 1
            return

        assignment = response.json()["assignments"].get(experiment)
        if assignment is None:
            tally.not_assigned += 1
            return

        variant = assignment["variant"]
        tally.assigned[variant] += 1
        await track(client, "/expose", body, tally)
        if rng.random() < rates.get(variant, 0.0):
            tally.converted[variant] += 1
            await track(client, "/convert", body, tally)


async def run(args: argparse.Namespace) -> Tally:
    tally = Tally()
    rng = random.Random(args.seed)
    limit = asyncio.Semaphore(args.concurrency)
    limits = httpx.Limits(max_connections=args.concurrency)
    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=REQUEST_TIMEOUT_SECONDS, limits=limits
    ) as client:
        await asyncio.gather(
            *(
                simulate_visitor(client, args.experiment, args.rates, rng, limit, tally)
                for _ in range(args.visitors)
            )
        )
    return tally


def report(tally: Tally, args: argparse.Namespace) -> None:
    total_assigned = sum(tally.assigned.values())
    print(f"\nvisitors: {args.visitors}")
    print(f"assigned: {total_assigned}, not assigned: {tally.not_assigned}")
    print(f"failed requests: {tally.failed_requests}")

    if total_assigned:
        print("\nvariant  visitors  share    conversions  rate     true rate")
    for variant in sorted(tally.assigned):
        count = tally.assigned[variant]
        conversions = tally.converted[variant]
        true_rate = args.rates.get(variant)
        print(
            f"{variant:<8} {count:<9} {100 * count / total_assigned:>5.2f}%   "
            f"{conversions:<12} {100 * conversions / count:>5.2f}%   "
            f"{'not set' if true_rate is None else f'{100 * true_rate:.2f}%'}"
        )

    latencies = sorted(tally.latencies_ms)
    if latencies:
        over_budget = sum(latency > ASSIGN_BUDGET_MS for latency in latencies)
        print(f"\nassign latency over {len(latencies)} requests, seen from this client:")
        print(f"p50: {percentile(latencies, 50):.1f} ms")
        print(f"p99: {percentile(latencies, 99):.1f} ms")
        print(f"over the snippet's {ASSIGN_BUDGET_MS} ms timeout: {over_budget}")

    # /expose and /convert always return 202, so these counts prove nothing was stored.
    print("\nCounts above are what was sent. Compare them with /dashboard for what was stored.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--rates", type=parse_rates, required=True, help="e.g. A=0.05,B=0.07")
    parser.add_argument("--visitors", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    tally = asyncio.run(run(args))
    report(tally, args)
    if not tally.assigned:
        sys.exit("\nNo visitor was assigned. Is the experiment running, with traffic above 0?")


if __name__ == "__main__":
    main()
