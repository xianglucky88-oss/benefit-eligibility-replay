"""Small deterministic property-test runner (stdlib only).

Mirrors the useful subset of a property framework: seeded generation across
several scenario modes, handcrafted edge cases, automatic shrinking of the
first failure and a structured report.  No third-party dependency is needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .generators import handcrafted_timelines, random_timeline
from .shrinker import is_minimal, shrink, timeline_size

MODES = ("general", "boundary", "retro", "duplicates", "appeal", "freeze")


@dataclass
class PropertyFailure:
    property_id: str
    seed: int
    mode: str
    detail: str
    original_size: int
    shrunk: object = None
    shrunk_size: int = 0
    minimal: bool = False


@dataclass
class PropertyReport:
    iterations: int
    passed: list[str] = field(default_factory=list)
    failures: list[PropertyFailure] = field(default_factory=list)
    errors: list[tuple[str, Exception]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures and not self.errors


def run_properties(property_map, iterations: int = 40,
                   base_seed: int = 20261116, do_shrink: bool = True,
                   verbose: bool = False) -> PropertyReport:
    report = PropertyReport(iterations=iterations)
    handcrafted = handcrafted_timelines()

    for property_id, check in property_map.items():
        failure = None
        # Handcrafted edge cases first: cheap and sharply diagnostic.
        for tl in handcrafted:
            ok, detail = _safe_check(check, tl)
            if not ok:
                failure = (tl, detail, "handcrafted", base_seed)
                break
        if failure is None:
            for i in range(iterations):
                seed = base_seed + i
                mode = MODES[i % len(MODES)]
                tl = random_timeline(seed, mode)
                try:
                    ok, detail = check(tl)
                except Exception as exc:  # an exception is a hard failure
                    report.errors.append((property_id, exc))
                    failure = (tl, f"exception: {exc!r}", mode, seed)
                    break
                if verbose and i % 10 == 0:
                    print(f"  {property_id} seed={seed} mode={mode} ok")
                if not ok:
                    failure = (tl, detail, mode, seed)
                    break

        if failure is None:
            report.passed.append(property_id)
            continue

        tl, detail, mode, seed = failure
        original_size = timeline_size(tl)
        shrunk, minimal = tl, False
        if do_shrink:
            try:
                shrunk = shrink(tl, check)
                minimal = is_minimal(shrunk, check)
            except Exception as exc:
                report.errors.append((property_id + " [shrink]", exc))
        report.failures.append(PropertyFailure(
            property_id=property_id, seed=seed, mode=mode, detail=detail,
            original_size=original_size, shrunk=shrunk,
            shrunk_size=timeline_size(shrunk), minimal=minimal,
        ))
    return report


def _safe_check(check, tl):
    try:
        return check(tl)
    except Exception as exc:
        return False, f"exception: {exc!r}"


def format_report(report: PropertyReport) -> str:
    lines = [
        "Property-test report",
        f"  iterations/property: {report.iterations}",
        f"  passed: {len(report.passed)}/{len(report.passed) + len(report.failures)}",
    ]
    for failure in report.failures:
        lines.append(
            f"  FAIL {failure.property_id} seed={failure.seed} "
            f"mode={failure.mode} size {failure.original_size}->"
            f"{failure.shrunk_size} minimal={failure.minimal}"
        )
        lines.append(f"       {failure.detail}")
    for property_id, exc in report.errors:
        lines.append(f"  ERROR {property_id}: {exc!r}")
    return "\n".join(lines)
