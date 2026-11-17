"""Minimal counter-example reduction for failing timelines.

Two-phase reduction:

1. ``ddmin`` over the removable atoms of a timeline (events, requests,
   policies, cases) – the classic delta-debugging algorithm that guarantees
   a 1-minimal subset with respect to atom removal.
2. A structured scalar sweep that shrinks day ordinals and monetary / member
   values while the failure persists.

The shrinker only ever produces structurally valid :class:`Timeline` objects;
it never edits assertions or the oracle.  A shrunk case is considered
minimal when removing any single remaining atom makes the property pass and
no scalar can be reduced further while keeping it red.
"""
from __future__ import annotations

from dataclasses import replace

from .timeline import (
    DecisionRequest,
    Event,
    Policy,
    Timeline,
)


def timeline_size(tl: Timeline) -> int:
    return (
        len(tl.cases) + len(tl.events) + len(tl.policies)
        + len(tl.requests)
    )


def _rebuild(tl: Timeline, keep: dict) -> Timeline:
    return Timeline(
        cases=tuple(c for i, c in enumerate(tl.cases) if keep.get(("c", i), True)),
        events=tuple(e for i, e in enumerate(tl.events) if keep.get(("e", i), True)),
        policies=tuple(p for i, p in enumerate(tl.policies) if keep.get(("p", i), True)),
        requests=tuple(r for i, r in enumerate(tl.requests) if keep.get(("r", i), True)),
    )


def _ddmin(tl: Timeline, is_failing) -> Timeline:
    # Atoms are tagged (kind, index); kind tags keep indices stable.
    atoms = (
        [("c", i) for i in range(len(tl.cases))]
        + [("e", i) for i in range(len(tl.events))]
        + [("p", i) for i in range(len(tl.policies))]
        + [("r", i) for i in range(len(tl.requests))]
    )

    def candidate(subset):
        keep = {atom: False for atom in atoms}
        for atom in subset:
            keep[atom] = True
        candidate_tl = _rebuild(tl, keep)
        try:
            ok, _ = is_failing(candidate_tl)
        except Exception:
            ok = False
        return not ok  # failing == worth keeping

    n = 2
    current = list(atoms)
    while len(current) >= 2:
        chunk = max(1, len(current) // n)
        reduced = False
        start = 0
        while start < len(current):
            piece = current[start:start + chunk]
            complement = current[:start] + current[start + chunk:]
            if candidate(complement):
                current = complement
                n = max(n - 1, 2)
                reduced = True
                break
            start += chunk
        if reduced:
            continue
        # Increasing granularity; test each single atom once n exceeds size.
        if n < len(current):
            n = min(len(current), n * 2)
            continue
        break
    keep = {atom: atom in set(current) for atom in atoms}
    return _rebuild(tl, keep)


def _scalar_candidates(event_or_request):
    """Yield (object, attr, smaller-value) attempts for numeric fields."""
    if isinstance(event_or_request, Event):
        for attr in ("at", "delivered_at"):
            value = getattr(event_or_request, attr)
            if isinstance(value, int):
                yield event_or_request, attr, max(0, value - 1)
        if "amount_cents" in event_or_request.payload:
            value = int(event_or_request.payload["amount_cents"])
            for smaller in (0, value // 2, value - 1):
                if 0 <= smaller < value:
                    payload = dict(event_or_request.payload)
                    payload["amount_cents"] = smaller
                    yield replace(event_or_request, payload=payload), None, None
        if "members" in event_or_request.payload:
            value = int(event_or_request.payload["members"])
            for smaller in (0, value - 1):
                if 0 <= smaller < value:
                    payload = dict(event_or_request.payload)
                    payload["members"] = smaller
                    yield replace(event_or_request, payload=payload), None, None
    elif isinstance(event_or_request, DecisionRequest):
        for attr in ("at", "notify_at", "period_start", "period_end"):
            value = getattr(event_or_request, attr)
            if isinstance(value, int):
                yield event_or_request, attr, max(0, value - 1)
    elif isinstance(event_or_request, Policy):
        for attr in ("effective_from", "enacted_at",
                     "income_limit_per_person", "benefit_monthly"):
            value = getattr(event_or_request, attr)
            if isinstance(value, int):
                yield event_or_request, attr, max(0, value - 1)


def _scalar_sweep(tl: Timeline, is_failing) -> Timeline:
    current = tl
    changed = True
    rounds = 0
    while changed and rounds < 4:
        changed = False
        rounds += 1
        for bucket_name, items in (
            ("events", current.events),
            ("requests", current.requests),
            ("policies", current.policies),
        ):
            for idx, item in enumerate(items):
                for attempt, attr, smaller in list(_scalar_candidates(item)):
                    if attr is None:
                        new_items = list(items)
                        new_items[idx] = attempt
                    else:
                        new_items = list(items)
                        new_items[idx] = replace(item, **{attr: smaller})
                    candidate_tl = replace(current, **{bucket_name: tuple(new_items)})
                    try:
                        ok, _ = is_failing(candidate_tl)
                    except Exception:
                        ok = False
                    if not ok:  # still failing -> keep the reduction
                        current = candidate_tl
                        items = getattr(current, bucket_name)
                        changed = True
                        break
    return current


def shrink(tl: Timeline, check) -> Timeline:
    """Reduce ``tl`` while ``check`` keeps failing.

    ``check`` has the property signature ``Timeline -> (ok, detail)``.
    """
    def is_failing(candidate):
        return check(candidate)

    reduced = _ddmin(tl, is_failing)
    reduced = _scalar_sweep(reduced, is_failing)
    # One final ddmin pass because scalar sweeps can expose new dead atoms.
    reduced = _ddmin(reduced, is_failing)
    return reduced


def is_minimal(tl: Timeline, check) -> bool:
    """A reduced timeline is atom-minimal if every single atom is needed."""
    atoms = (
        [("c", i) for i in range(len(tl.cases))]
        + [("e", i) for i in range(len(tl.events))]
        + [("p", i) for i in range(len(tl.policies))]
        + [("r", i) for i in range(len(tl.requests))]
    )
    for atom in atoms:
        keep = {a: True for a in atoms}
        keep[atom] = False
        candidate = _rebuild(tl, keep)
        ok, _ = check(candidate)
        if not ok:
            return False
    return True
