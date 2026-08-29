"""Same 500 cases, same runtime, two classifiers.

One variable only: who answers the classification question. Evidence verification is left OFF
in both arms so the deterministic arm reproduces the frozen V2 numbers exactly and any
difference is attributable to the classifier and to nothing else.

On a refusal nothing is executed and the live agent is not run: this experiment measures what
Rote does with an answer, not how well a fallback agent works.
"""

from __future__ import annotations

import collections
import itertools
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from rote.bootstrap.system import CompiledSystem, policy_context, session_gate
from rote.contracts.checker import CheckerVerdict
from rote.contracts.classifier import ClassifierModel
from rote.contracts.common import Domain, ExceptionCategory
from rote.contracts.errors import ClassifierError
from rote.contracts.execution import ExecutionOutcome
from rote.contracts.policy import ExecutionPath, PolicyContext
from rote.contracts.reconciliation import GeneratedDataset, ReconciliationException
from rote.contracts.routing import RouteKind, RouteReason
from rote.domain.checkers.reconciliation import check_outcome
from rote.domain.tools.adapters import ReconciliationTools
from rote.runtime.classifier import Classifier
from rote.runtime.evidence_check import VerificationOutcome, verify
from rote.runtime.executor import execute_plan
from rote.runtime.guard import Guard, default_guard_config
from rote.runtime.router import DEFAULT_MIN_CONFIDENCE_PER_MILLE, Router
from rote.safety.ledger import Ledger

CONFIDENCE_BUCKETS: tuple[int, ...] = (0, 200, 400, 600, 700, 800, 900, 1000)


@dataclass(frozen=True)
class CaseRecord:
    exception_id: str
    true_category: str
    said_category: str
    confidence_per_mille: int
    route_reason: str
    automated: bool
    verdict: str
    plan_lookups: int
    steps_executed: int
    classifier_failed: bool
    elapsed_ms: int
    verification: str = "not_run"


@dataclass
class ArmResult:
    label: str
    model_id: str
    cases: int = 0
    classifications_correct: int = 0
    classifications_scored: int = 0
    automated: int = 0
    refused: int = 0
    wrong_automated_actions: int = 0
    undetermined_automated: int = 0
    plan_lookups: int = 0
    executions: int = 0
    steps_executed: int = 0
    provider_failures: int = 0
    verification_refusals: int = 0
    verification_mismatch: int = 0
    verification_unverifiable: int = 0
    classifier_calls: int = 0
    untrusted_withheld: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latencies_ms: list[int] = field(default_factory=list)
    route_reasons: collections.Counter[str] = field(default_factory=collections.Counter)
    confusion: collections.Counter[tuple[str, str]] = field(default_factory=collections.Counter)
    confidence: collections.Counter[str] = field(default_factory=collections.Counter)
    ledger_entries: int = 0
    ledger_valid: bool = True
    records: list[CaseRecord] = field(default_factory=list)

    @property
    def coverage_per_mille(self) -> int:
        return round(1000 * self.automated / self.cases) if self.cases else 0

    @property
    def accuracy_per_mille(self) -> int:
        if not self.classifications_scored:
            return 0
        return round(1000 * self.classifications_correct / self.classifications_scored)

    @property
    def median_latency_ms(self) -> int:
        return int(statistics.median(self.latencies_ms)) if self.latencies_ms else 0

    @property
    def total_latency_ms(self) -> int:
        return sum(self.latencies_ms)


def run_arm(
    *,
    label: str,
    system: CompiledSystem,
    dataset: GeneratedDataset,
    model: ClassifierModel,
    limit: int | None = None,
    verify_evidence: bool = False,
) -> ArmResult:
    exceptions: Sequence[ReconciliationException] = dataset.exceptions[
        : limit or len(dataset.exceptions)
    ]
    truths = {truth.exception_id: truth for truth in dataset.ground_truths}

    adapters = ReconciliationTools.from_snapshot(dataset.world)
    ledger = Ledger()
    gate = session_gate(adapters, ledger)
    classifier = Classifier(model=model)
    router = Router(
        plans=system.registry,
        domain=Domain.RECONCILIATION,
        min_confidence_per_mille=DEFAULT_MIN_CONFIDENCE_PER_MILLE,
    )
    result = ArmResult(label=label, model_id=model.model_id)
    withheld_total = 0

    for exception in exceptions:
        facts = exception.facts.model_dump(mode="json")
        truth = truths[exception.exception_id]
        # D5 again, in the evaluator: a model that may not read free text is not shown it
        untrusted = exception.untrusted if model.is_local else ()
        withheld_total += len(exception.untrusted) - len(untrusted)

        started = time.perf_counter()
        failed = False
        if verify_evidence:
            checked = verify(
                exception.facts,
                gate.for_task(
                    PolicyContext(
                        task_id=exception.exception_id,
                        correlation_id=f"{exception.exception_id}:verification",
                        path=ExecutionPath.COMPILED_PLAN,
                        category=None,
                        actor="system:verifier",
                    )
                ),
            )
            if checked.outcome is not VerificationOutcome.AGREEMENT:
                mismatch = checked.outcome is VerificationOutcome.MISMATCH
                result.verification_refusals += 1
                if mismatch:
                    result.verification_mismatch += 1
                else:
                    result.verification_unverifiable += 1
                result.cases += 1
                result.refused += 1
                reason_name = (
                    RouteReason.EVIDENCE_MISMATCH.value
                    if mismatch
                    else RouteReason.EVIDENCE_UNVERIFIABLE.value
                )
                result.route_reasons[reason_name] += 1
                elapsed = int((time.perf_counter() - started) * 1000)
                result.latencies_ms.append(elapsed)
                result.records.append(
                    CaseRecord(
                        exception_id=exception.exception_id,
                        true_category=truth.category.value,
                        said_category="",
                        confidence_per_mille=0,
                        route_reason=reason_name,
                        automated=False,
                        verdict="not_attempted",
                        plan_lookups=0,
                        steps_executed=0,
                        classifier_failed=False,
                        elapsed_ms=elapsed,
                        verification=checked.outcome.value,
                    )
                )
                continue
        try:
            classification = classifier.classify(facts, untrusted, exception.exception_id)
            route = router.route(facts, classification)
            said = classification.category.value
            confidence = classification.confidence_per_mille
            reason = route.reason
        except Exception:
            # a provider failure is a refusal, never a crash and never a different classifier
            failed = True
            said = ExceptionCategory.UNKNOWN.value
            confidence = 0
            reason = RouteReason.CLASSIFIER_UNAVAILABLE
            route = None
        elapsed = int((time.perf_counter() - started) * 1000)

        result.cases += 1
        result.classifier_calls += 1
        result.latencies_ms.append(elapsed)
        result.route_reasons[reason.value] += 1
        result.confusion[(truth.category.value, said)] += 1
        result.confidence[_bucket(confidence)] += 1
        if failed:
            result.provider_failures += 1
        else:
            result.classifications_scored += 1
            if said == truth.category.value:
                result.classifications_correct += 1

        automated = False
        verdict = CheckerVerdict.UNDETERMINED
        lookups = 0
        steps = 0
        if route is not None and route.kind is RouteKind.COMPILED_PLAN and route.plan_id:
            lookups = 1
            plan = system.registry.get(route.plan_id, route.plan_version or 1)
            executed = execute_plan(
                plan=plan,
                task_input=facts,
                toolbox=gate.for_task(
                    policy_context(
                        exception.exception_id, ExecutionPath.COMPILED_PLAN, plan.category
                    )
                ),
                inspector=Guard(config=default_guard_config()),
            )
            steps = executed.steps_completed
            automated = executed.outcome is ExecutionOutcome.RESOLVED
            if automated:
                verdict = check_outcome(exception.facts, truth, adapters.snapshot()).verdict

        result.plan_lookups += lookups
        result.steps_executed += steps
        if automated:
            result.automated += 1
            result.executions += 1
            if verdict is CheckerVerdict.FAIL:
                result.wrong_automated_actions += 1
            elif verdict is CheckerVerdict.UNDETERMINED:
                result.undetermined_automated += 1
        else:
            result.refused += 1

        result.records.append(
            CaseRecord(
                exception_id=exception.exception_id,
                true_category=truth.category.value,
                said_category=said,
                confidence_per_mille=confidence,
                route_reason=reason.value,
                automated=automated,
                verdict=verdict.value,
                plan_lookups=lookups,
                steps_executed=steps,
                classifier_failed=failed,
                elapsed_ms=elapsed,
            )
        )

    result.untrusted_withheld = withheld_total
    result.tokens_in = _counter_of(model, "tokens_in")
    result.tokens_out = _counter_of(model, "tokens_out")
    result.ledger_entries = len(ledger.entries)
    result.ledger_valid = bool(ledger.verify().valid)
    return result


def summary(result: ArmResult) -> dict[str, Any]:
    return {
        "label": result.label,
        "model_id": result.model_id,
        "cases": result.cases,
        "classification_accuracy_per_mille": result.accuracy_per_mille,
        "classifications_scored": result.classifications_scored,
        "automation_coverage_per_mille": result.coverage_per_mille,
        "automated": result.automated,
        "refused": result.refused,
        "wrong_automated_actions": result.wrong_automated_actions,
        "undetermined_automated": result.undetermined_automated,
        "plan_lookups": result.plan_lookups,
        "executions": result.executions,
        "compiled_steps_executed": result.steps_executed,
        "provider_failures": result.provider_failures,
        "verification_refusals": result.verification_refusals,
        "verification_mismatch": result.verification_mismatch,
        "verification_unverifiable": result.verification_unverifiable,
        "classifier_calls": result.classifier_calls,
        "untrusted_withheld": result.untrusted_withheld,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "median_classification_ms": result.median_latency_ms,
        "total_classification_ms": result.total_latency_ms,
        "route_reasons": dict(sorted(result.route_reasons.items())),
        "confidence_buckets": dict(sorted(result.confidence.items())),
        "confusion": {f"{true}->{said}": n for (true, said), n in sorted(result.confusion.items())},
        "ledger_entries": result.ledger_entries,
        "ledger_valid": result.ledger_valid,
    }


def _bucket(confidence: int) -> str:
    for low, high in itertools.pairwise(CONFIDENCE_BUCKETS):
        if low <= confidence < high:
            return f"{low}-{high}"
    return f"{CONFIDENCE_BUCKETS[-1]}"


def _counter_of(model: ClassifierModel, name: str) -> int:
    value = getattr(model, name, 0)
    return value if isinstance(value, int) else 0


__all__ = ["ArmResult", "CaseRecord", "ClassifierError", "run_arm", "summary"]
