"""Fault injection and fault localisation matrix.

Each mutant injects exactly one fault into a single, named component.  The
matrix records the fault class (time comparison / event de-duplication /
policy selection / report field mapping, plus money, evidence and freeze
faults) and the property that must catch it first.  The QA test suite proves
two things for every mutant:

1. the correct (unmutated) implementation passes every property;
2. each mutant is *killed* and *localised* – the first failing property is
   exactly the one named in the matrix, so a failure tells the reviewer which
   component and which time semantic is wrong rather than just saying
   "something broke".

Oracle mutants replace a swappable :class:`~qa_support.timeline.Components`
seam or wrap ``replay``; SUT mutants monkeypatch one production function.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import timeline as tl_mod
from .timeline import (
    Components,
    replay,
)

FAULT_TIME_COMPARISON = "time_comparison"
FAULT_DEDUP = "event_dedup"
FAULT_POLICY_SELECTION = "policy_selection"
FAULT_REPORT_MAPPING = "report_field_mapping"
FAULT_ROUNDING = "rounding"
FAULT_EVIDENCE = "evidence_gating"
FAULT_FREEZE = "notification_freeze"


# ---------------------------------------------------------------------------
# Oracle-side faulty component implementations
# ---------------------------------------------------------------------------
def _facts_at_ignoring_effective_time(events, case, day):
    """Fault: compare intake time instead of evidence-effective time.

    The ``at <= day`` guard is dropped, so future-dated corrections leak into
    decisions that should have been made without them.
    """
    income, household, docs, has_app = 0, 0, [], False
    for event in events:
        if event.case != case or not event.valid:
            continue
        if event.kind == "income":
            income += int(event.payload["amount_cents"])
        elif event.kind == "household":
            household += int(event.payload["members"])
        elif event.kind == "evidence":
            docs.append(str(event.payload.get("doc", "")))
        elif event.kind == "application":
            has_app = True
    return income, household, tuple(docs), has_app


def _dedup_keeps_last(events):
    """Fault: redelivery overwrites the original record and re-enters
    evaluation, so a repeated (possibly mutated) delivery changes facts.

    The correct dedup keeps the first occurrence and ignores the rest.  This
    mutant emits every record into the active stream (only marking exact
    re-delivery as a duplicate), so a payload-mutated redelivery overwrites
    or adds to the original fact when the events are later summed.
    """
    seen: dict = {}
    duplicates = []
    active = []
    for event in events:
        if not event.valid:
            active.append(event)
            continue
        key = event.natural_key()
        if key in seen:
            duplicates.append(key)
            # Mutation: the redelivery is NOT ignored – it is evaluated too.
            active.append(event)
        else:
            seen[key] = event
            active.append(event)
    return active, duplicates


def _policy_selector_ignores_enactment(policies, evidence_day, decision_day):
    """Fault: choose by effective range only, ignoring enactment time."""
    candidates = [p for p in policies if p.in_force_at(evidence_day)]
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: (-p.effective_from, p.policy_id))[0]


def _rounder_truncate(numerator, denominator):
    """Fault: truncate instead of half-up rounding."""
    return abs(numerator) // denominator


def _evidence_check_disabled(_has_application_evidence):
    """Fault: missing application evidence no longer blocks a decision."""
    return True


def _replay_with_unfrozen_notifications(timeline, components=None):
    """Fault: re-decisions rewrite the historical notification record."""
    result = replay(timeline, components)
    if not result.notifications:
        return result
    latest = result.latest_versions()
    notes = []
    for note in result.notifications:
        decision = latest[(note.case, note.period)]
        # Mutation: overwrite the frozen snapshot with the newest entitlement.
        notes.append(type(note)(
            case=note.case, period=note.period, version=note.version,
            notified_at=note.notified_at,
            benefit_cents=decision.benefit_cents,
            status=decision.status,
        ))
    return type(result)(
        decisions=result.decisions, notifications=tuple(notes),
        adjustments=result.adjustments, rejected_events=result.rejected_events,
        duplicate_events=result.duplicate_events, requests=result.requests,
        appeals_upheld=result.appeals_upheld,
    )


def _replay_with_swapped_report(timeline, components=None):
    """Fault: report maps the entitlement field onto the status flag.

    The corrupted rows are stashed on the replay result so that downstream
    consumers (and P14) observe the swapped mapping.
    """
    result = replay(timeline, components)
    rows = result.report_rows()
    for row in rows:
        row["final_entitlement_cents"], row["final_status"] = (
            row["final_status"], row["final_entitlement_cents"]
        )
    return type(result)(
        decisions=result.decisions,
        notifications=result.notifications,
        adjustments=result.adjustments,
        rejected_events=result.rejected_events,
        duplicate_events=result.duplicate_events,
        requests=result.requests,
        appeals_upheld=result.appeals_upheld,
        faulty_report=rows,
    )


# ---------------------------------------------------------------------------
# Mutant catalogue
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Mutant:
    mutant_id: str
    target: str                       # dotted component/function name
    fault_class: str
    primary_property: str             # must fail first -> localisation
    runner: Callable                  # Timeline -> ReplayResult-like


def _component_runner(components: Components):
    def run(timeline, _components=None):
        return replay(timeline, components)
    return run


def _run_with_faulty_facts(timeline, components=None):
    """Temporarily patch the oracle's fact lookup and replay once."""
    original = tl_mod._facts_at
    tl_mod._facts_at = _facts_at_ignoring_effective_time
    try:
        return replay(timeline, components)
    finally:
        tl_mod._facts_at = original


ORACLE_MUTANTS = (
    Mutant(
        "M1_time_effective_guard_dropped",
        "qa_support.timeline._facts_at",
        FAULT_TIME_COMPARISON,
        "P1_timestamp_separation",
        _run_with_faulty_facts,
    ),
    Mutant(
        "M2_dedup_last_write_wins",
        "qa_support.timeline.default_dedup",
        FAULT_DEDUP,
        "P3_redelivery_dedup_invariance",
        _component_runner(Components(dedup=_dedup_keeps_last)),
    ),
    Mutant(
        "M3_policy_ignores_enactment",
        "qa_support.timeline.default_policy_selector",
        FAULT_POLICY_SELECTION,
        "P5_retro_policy_scope",
        _component_runner(Components(policy_selector=_policy_selector_ignores_enactment)),
    ),
    Mutant(
        "M4_rounding_truncates",
        "qa_support.timeline.default_round_half_up",
        FAULT_ROUNDING,
        "P12_cent_exact_rounding",
        _component_runner(Components(rounder=_rounder_truncate)),
    ),
    Mutant(
        "M5_evidence_check_disabled",
        "qa_support.timeline.default_evidence_check",
        FAULT_EVIDENCE,
        "P8_evidence_gating",
        _component_runner(Components(evidence_check=_evidence_check_disabled)),
    ),
    Mutant(
        "M6_notification_snapshot_rewritten",
        "qa_support.timeline.replay.notifications",
        FAULT_FREEZE,
        "P6_notification_freeze",
        _replay_with_unfrozen_notifications,
    ),
    Mutant(
        "M7_report_fields_swapped",
        "qa_support.timeline.ReplayResult.report_rows",
        FAULT_REPORT_MAPPING,
        "P14_report_field_mapping",
        _replay_with_swapped_report,
    ),
)


# ---------------------------------------------------------------------------
# SUT (production module) mutants – one fault per production function
# ---------------------------------------------------------------------------
def make_sut_mutants():
    """Build mutants targeting parser / engine / report / pipeline.

    Imported lazily so importing this module never touches production code.
    """
    from benefit_eligibility_replay import parser as sut_parser
    from benefit_eligibility_replay import engine as sut_engine
    from benefit_eligibility_replay import report as sut_report
    from benefit_eligibility_replay import pipeline as sut_pipeline

    def faulty_parser_load_records(payload):
        """Fault: row-copy semantics removed, callers can mutate intake state.

        The documented contract returns independent ``dict`` copies.  This
        mutant returns the original row objects, so an in-place mutation by
        one consumer corrupts every later parse/use – a classic event
        de-duplication / shared-state fault at the intake boundary.
        """
        rows = payload.get("applications", [])
        if not isinstance(rows, list):
            raise ValueError("applications must be a list")
        return [row for row in rows if isinstance(row, dict)]

    def faulty_engine_calculate(rows, payload):
        """Fault: the eligibility time/order comparison is flipped.

        The correct rule is ``income <= limit`` (an application exactly at
        the limit is eligible).  This mutant uses ``income < limit``, so the
        boundary decision – the one the welfare review team cares about –
        silently flips to ineligible.
        """
        policy = payload["policy"]
        decisions = []
        changes = {c["case"]: c for c in payload.get("changes", [])}
        for row in rows:
            final_income = changes.get(row["case"], {}).get(
                "income", row["income"]
            )
            limit = row["household"] * policy["income_limit_per_person"]
            eligible = final_income < limit  # mutated comparison
            decisions.append({
                "case": row["case"], "eligible": eligible,
                "income": final_income, "limit": limit,
                "benefit": policy["benefit"] if eligible else 0,
                "evidence": (["application", "change"]
                             if row["case"] in changes else ["application"]),
            })
        return {
            "decisions": decisions,
            "eligible_count": sum(d["eligible"] for d in decisions),
        }

    def faulty_report_build_result(project, data):
        """Fault: status/field mapping swaps two envelope fields."""
        result = {
            "project": project, "status": "ok", **data,
        }
        if "eligible_count" in result:
            result["eligible_count"] = -int(result["eligible_count"])
        return result

    original_pipeline_run = sut_pipeline.run

    def faulty_pipeline_run(payload):
        """Fault: pipeline silently swallows invalid input instead of raising."""
        try:
            return original_pipeline_run(payload)
        except (KeyError, ValueError, TypeError):
            return {"project": "benefit-eligibility-replay",
                    "status": "ok", "decisions": [], "eligible_count": 0}

    return (
        Mutant(
            "S1_parser_shared_row_objects",
            "benefit_eligibility_replay.parser.load_records",
            FAULT_DEDUP,
            "SUT_parser_copy_semantics",
            ("patch", sut_parser, "load_records", faulty_parser_load_records),
        ),
        Mutant(
            "S2_engine_boundary_comparison_flipped",
            "benefit_eligibility_replay.engine.calculate",
            FAULT_TIME_COMPARISON,
            "SUT_engine_boundary",
            ("patch", sut_engine, "calculate", faulty_engine_calculate),
        ),
        Mutant(
            "S3_report_field_mapping_negated",
            "benefit_eligibility_replay.report.build_result",
            FAULT_REPORT_MAPPING,
            "SUT_report_mapping",
            ("patch", sut_report, "build_result", faulty_report_build_result),
        ),
        Mutant(
            "S4_pipeline_swallows_errors",
            "benefit_eligibility_replay.pipeline.run",
            FAULT_TIME_COMPARISON,
            "SUT_pipeline_error_propagation",
            ("patch", sut_pipeline, "run", faulty_pipeline_run),
        ),
    )


class PatchSession:
    """Context manager that monkeypatches one attribute and restores it."""

    def __init__(self, module, attr, replacement):
        self.module = module
        self.attr = attr
        self.replacement = replacement
        self.original = None

    def __enter__(self):
        self.original = getattr(self.module, self.attr)
        setattr(self.module, self.attr, self.replacement)
        return self

    def __exit__(self, *exc):
        setattr(self.module, self.attr, self.original)
        return False


def run_mutant(mutant: Mutant, *args):
    """Invoke a mutant.  Oracle mutants take a timeline; SUT mutants take
    whatever payload the SUT property supplies."""
    spec = mutant.runner
    if isinstance(spec, tuple) and spec[0] == "patch":
        _, module, attr, replacement = spec
        with PatchSession(module, attr, replacement):
            return replacement(*args)
    return spec(*args)


# ---------------------------------------------------------------------------
# Localisation harness
# ---------------------------------------------------------------------------
def evaluate_oracle_mutants(property_map, timelines, property_order):
    """Run every oracle mutant and return its localisation verdict.

    Localisation means the mutant's *named primary property* fails on at
    least one timeline – i.e. a reviewer running that property is pointed
    straight at the faulty component/time semantic.  The verdict also records
    every property the mutant breaks, so cross-fault sensitivity is visible
    rather than hidden behind an arbitrary property ordering.
    """
    from .properties import evaluate_property

    verdicts = {}
    for mutant in ORACLE_MUTANTS:
        failing: set[str] = set()
        raised = None
        for tl in timelines:
            for pid in property_order:
                if pid in failing:
                    continue
                try:
                    ok, _detail = evaluate_property(pid, tl, mutant.runner)
                except Exception as exc:  # a crash also kills the mutant
                    ok, raised = False, repr(exc)
                if not ok:
                    failing.add(pid)
            if mutant.primary_property in failing:
                break  # localisation already proven
        verdicts[mutant.mutant_id] = {
            "mutant": mutant,
            "killed": bool(failing),
            "failing_properties": tuple(sorted(failing)),
            "expected": mutant.primary_property,
            "localised": mutant.primary_property in failing,
            "raised": raised,
        }
    return verdicts
