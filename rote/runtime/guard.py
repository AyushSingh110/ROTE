from __future__ import annotations

from typing import Any

from rote.contracts.fingerprint import structural_fingerprint, structural_schema
from rote.contracts.guard import (
    FULL_SCALE,
    GuardConfig,
    GuardSignal,
    GuardVerdict,
    GuardWeights,
    SignalScore,
)
from rote.contracts.invariants import evaluate_invariants
from rote.contracts.paths import enumerate_paths
from rote.contracts.plan import PlanStep, StepExpectation

RESULT_SIGNALS = (
    GuardSignal.STRUCTURAL,
    GuardSignal.NUMERIC,
    GuardSignal.CATEGORICAL,
    GuardSignal.BEHAVIOURAL,
)


def default_guard_config() -> GuardConfig:
    return GuardConfig(
        weights=GuardWeights(structural=350, numeric=250, categorical=250, behavioural=150),
        threshold_per_mille=500,
        retry_penalty_per_mille=300,
        added_key_penalty_per_mille=400,
    )


class Guard:
    def __init__(self, *, config: GuardConfig) -> None:
        self._config = config
        self._inspections: list[GuardVerdict] = []

    @property
    def inspections(self) -> tuple[GuardVerdict, ...]:
        return tuple(self._inspections)

    # runs after the arguments are resolved and BEFORE the gate, because an invariant checked
    # after the money moved prevents nothing
    def check_proposed_action(
        self, step: PlanStep, arguments: dict[str, Any], task_input: dict[str, Any]
    ) -> GuardVerdict:
        failed = evaluate_invariants(step.expect.invariants, arguments, task_input)
        return self._record(
            GuardVerdict(
                passed=not failed,
                reason="invariant veto" if failed else "",
                step_index=step.index,
                checkpoint="proposed_action",
                divergence_per_mille=0,
                threshold_per_mille=self._config.threshold_per_mille,
                scores=(
                    SignalScore(
                        signal=GuardSignal.INVARIANT,
                        score_per_mille=FULL_SCALE if failed else 0,
                        detail=", ".join(failed),
                    ),
                ),
                failed_invariants=failed,
                vetoed=bool(failed),
            )
        )

    def check_result(
        self, step: PlanStep, result: dict[str, Any], attempts: int = 1
    ) -> GuardVerdict:
        expect = step.expect
        scores = (
            SignalScore(**self._structural(expect, result)),
            SignalScore(**self._numeric(expect, result)),
            SignalScore(**self._categorical(expect, result)),
            SignalScore(**self._behavioural(attempts)),
        )
        divergence = (
            sum(
                score.score_per_mille * self._config.weights.weight_for(score.signal)
                for score in scores
            )
            // FULL_SCALE
        )
        return self._record(
            GuardVerdict(
                passed=divergence < self._config.threshold_per_mille,
                reason=_summarise(scores) if divergence else "",
                step_index=step.index,
                checkpoint="result",
                divergence_per_mille=divergence,
                threshold_per_mille=self._config.threshold_per_mille,
                scores=scores,
            )
        )

    # satisfies the executor's inspector protocol
    def inspect(self, step: PlanStep, result: dict[str, Any], attempts: int = 1) -> GuardVerdict:
        return self.check_result(step, result, attempts)

    def _structural(self, expect: StepExpectation, result: dict[str, Any]) -> dict[str, Any]:
        signal = {"signal": GuardSignal.STRUCTURAL}
        if structural_fingerprint(result) in expect.result_fingerprints:
            return {**signal, "score_per_mille": 0, "detail": ""}
        if not expect.schema_always:
            return {**signal, "score_per_mille": FULL_SCALE, "detail": "unseen result shape"}

        observed = {f"{path}|{kind}" for path, kind in structural_schema(result)}
        missing = sorted(expect.schema_always - observed)
        if missing:
            return {**signal, "score_per_mille": FULL_SCALE, "detail": f"missing {missing[0]}"}
        added = sorted(observed - expect.schema_ever)
        if added:
            return {
                **signal,
                "score_per_mille": self._config.added_key_penalty_per_mille,
                "detail": f"added {added[0]}",
            }
        return {**signal, "score_per_mille": 0, "detail": ""}

    def _numeric(self, expect: StepExpectation, result: dict[str, Any]) -> dict[str, Any]:
        found = enumerate_paths(result)
        worst, detail = 0, ""
        for path, (low, high) in sorted(expect.numeric_widened.items()):
            value = found.get(path)
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            outside = max(low - value, value - high, 0)
            if outside <= 0:
                continue
            half_width = max(1, (high - low) // 2)
            score = min(FULL_SCALE, outside * FULL_SCALE // half_width)
            if score > worst:
                worst, detail = score, f"{path}={value} outside [{low},{high}]"
        return {"signal": GuardSignal.NUMERIC, "score_per_mille": worst, "detail": detail}

    def _categorical(self, expect: StepExpectation, result: dict[str, Any]) -> dict[str, Any]:
        found = enumerate_paths(result)
        for path, domain in sorted(expect.categorical_domains.items()):
            value = found.get(path)
            if isinstance(value, str) and value not in domain:
                return {
                    "signal": GuardSignal.CATEGORICAL,
                    "score_per_mille": FULL_SCALE,
                    "detail": f"{path}={value!r} was never seen",
                }
        return {"signal": GuardSignal.CATEGORICAL, "score_per_mille": 0, "detail": ""}

    def _behavioural(self, attempts: int) -> dict[str, Any]:
        if attempts <= 1:
            return {"signal": GuardSignal.BEHAVIOURAL, "score_per_mille": 0, "detail": ""}
        return {
            "signal": GuardSignal.BEHAVIOURAL,
            "score_per_mille": self._config.retry_penalty_per_mille,
            "detail": f"succeeded after {attempts} attempts",
        }

    def _record(self, verdict: GuardVerdict) -> GuardVerdict:
        self._inspections.append(verdict)
        return verdict


def _summarise(scores: tuple[SignalScore, ...]) -> str:
    fired = [score for score in scores if score.score_per_mille > 0]
    return "; ".join(f"{score.signal.value}: {score.detail}" for score in fired)
