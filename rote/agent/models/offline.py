from __future__ import annotations

import random
from typing import Any

from rote.contracts.agent import AgentDecision, DecisionRequest, ModelResponse

# test double. It reasons only from what the agent can observe and never sees ground truth,
# so it can be wrong and the checker will say so. Compilability measured on its trajectories
# alone is not a research result: ARCHITECTURE.md SS I.8 requires agreement with a real model.
MODEL_ID = "offline-heuristic-1"
PROMPT_TEMPLATE_ID = "offline-v1"

SEARCH_WINDOW_DAYS = 5
BPS_DIVISOR = 10_000
MICROS = 1_000_000


class OfflineHeuristicModel:
    model_id = MODEL_ID
    prompt_template_id = PROMPT_TEMPLATE_ID

    def __init__(self, seed: int, exploration: float = 0.0) -> None:
        self._rng = random.Random(seed)
        self._exploration = exploration

    def decide(self, request: DecisionRequest) -> ModelResponse:
        return ModelResponse(decision=self._next(request), tokens_in=0, tokens_out=0)

    def _next(self, request: DecisionRequest) -> AgentDecision:
        facts = request.task_input
        offered = {spec.name for spec in request.available_tools}
        done = {observation.tool for observation in request.observations}

        detour = self._detour(request, facts, offered, done)
        if detour is not None:
            return detour

        if "get_settlement_record" not in done:
            return _call("get_settlement_record", {"record_id": facts["record_id"]})

        bank = facts["bank_amount"]
        internal = facts["internal_amount"]
        if bank is None:
            return _finish_or_escalate("no bank line was presented with this exception")

        target = self._target_line(request, facts, done)
        if target is None:
            return _call(
                "find_bank_lines_by_amount",
                {
                    "minor_units": internal["minor_units"],
                    "currency": internal["currency"],
                    "around_date": facts["captured_on"],
                    "window_days": SEARCH_WINDOW_DAYS,
                },
            )

        if bank["currency"] != internal["currency"]:
            return self._resolve_cross_currency(request, facts, done, target)
        if bank["minor_units"] < internal["minor_units"]:
            return self._resolve_short_credit(request, facts, done, target)
        if _looks_duplicated(facts):
            return self._resolve_duplicate(facts, done, target)
        return self._close(facts, done, target, "matched")

    def _detour(
        self,
        request: DecisionRequest,
        facts: dict[str, Any],
        offered: set[str],
        done: set[str],
    ) -> AgentDecision | None:
        if self._rng.random() >= self._exploration:
            return None
        if "get_merchant_profile" in offered and "get_merchant_profile" not in done:
            return _call("get_merchant_profile", {"merchant_id": facts["merchant_id"]})
        record = _result_of(request, "get_settlement_record")
        if (
            "get_chargeback_history" in offered
            and "get_chargeback_history" not in done
            and record is not None
        ):
            return _call("get_chargeback_history", {"order_id": record["record"]["order_id"]})
        return None

    def _target_line(
        self, request: DecisionRequest, facts: dict[str, Any], done: set[str]
    ) -> str | None:
        candidates: list[str] = list(facts["candidate_bank_line_ids"])
        if len(candidates) == 1 or _looks_duplicated(facts):
            return candidates[0]
        search = _result_of(request, "find_bank_lines_by_amount")
        if search is None:
            return None
        hits = [line_id for line_id in search["line_ids"] if line_id in candidates]
        return hits[0] if hits else candidates[0]

    def _resolve_cross_currency(
        self,
        request: DecisionRequest,
        facts: dict[str, Any],
        done: set[str],
        target: str,
    ) -> AgentDecision:
        internal = facts["internal_amount"]
        bank = facts["bank_amount"]
        if "get_fx_rate" not in done:
            return _call(
                "get_fx_rate",
                {
                    "base": internal["currency"],
                    "quote": bank["currency"],
                    "rate_date": facts["bank_value_date"],
                },
            )
        rate = _result_of(request, "get_fx_rate")
        if rate is None:
            return _finish_or_escalate("the exchange rate lookup produced nothing")
        expected = internal["minor_units"] * rate["rate"]["rate_micros"] // MICROS
        if "post_adjustment" not in done:
            return _post(facts, expected - bank["minor_units"], bank["currency"], "fx_rounding")
        return self._close(facts, done, target, "matched")

    def _resolve_short_credit(
        self,
        request: DecisionRequest,
        facts: dict[str, Any],
        done: set[str],
        target: str,
    ) -> AgentDecision:
        internal = facts["internal_amount"]
        bank = facts["bank_amount"]
        if "get_fee_schedule" not in done:
            return _call("get_fee_schedule", {"merchant_id": facts["merchant_id"]})
        schedule = _result_of(request, "get_fee_schedule")
        if schedule is None:
            return _finish_or_escalate("the fee schedule lookup produced nothing")
        shortfall = internal["minor_units"] - bank["minor_units"]
        fee = _scheduled_fee(schedule["schedule"], internal["minor_units"])
        explained_by_fee = fee == shortfall
        if "post_adjustment" not in done:
            reason = "fee" if explained_by_fee else "shortfall"
            return _post(facts, shortfall, bank["currency"], reason)
        status = "matched" if explained_by_fee else "partially_settled"
        return self._close(facts, done, target, status)

    def _resolve_duplicate(
        self, facts: dict[str, Any], done: set[str], target: str
    ) -> AgentDecision:
        candidates: list[str] = list(facts["candidate_bank_line_ids"])
        if "void_duplicate_bank_line" not in done:
            return _call(
                "void_duplicate_bank_line",
                {
                    "line_id": candidates[1],
                    "idempotency_key": f"{facts['record_id']}:void",
                },
            )
        return self._close(facts, done, target, "matched")

    def _close(
        self, facts: dict[str, Any], done: set[str], target: str, status: str
    ) -> AgentDecision:
        if "mark_settlement_matched" in done:
            return AgentDecision(action="finish", reason="the settlement is closed")
        return _call(
            "mark_settlement_matched",
            {
                "record_id": facts["record_id"],
                "bank_line_id": target,
                "status": status,
                "idempotency_key": f"{facts['record_id']}:close",
            },
        )


def _looks_duplicated(facts: dict[str, Any]) -> bool:
    return (
        len(facts["candidate_bank_line_ids"]) == 2
        and facts["bank_narration_reference"] == facts["internal_reference"]
    )


def _scheduled_fee(schedule: dict[str, Any], internal_minor_units: int) -> int:
    flat = int(schedule["flat_fee_minor_units"])
    bps = int(schedule["percentage_bps"])
    return flat + internal_minor_units * bps // BPS_DIVISOR


def _post(facts: dict[str, Any], minor_units: int, currency: str, reason: str) -> AgentDecision:
    return _call(
        "post_adjustment",
        {
            "record_id": facts["record_id"],
            "minor_units": minor_units,
            "currency": currency,
            "reason": reason,
            "idempotency_key": f"{facts['record_id']}:adjust",
        },
    )


def _call(tool: str, arguments: dict[str, Any]) -> AgentDecision:
    return AgentDecision(action="call_tool", tool=tool, arguments=arguments)


def _finish_or_escalate(reason: str) -> AgentDecision:
    return AgentDecision(action="escalate", reason=reason)


def _result_of(request: DecisionRequest, tool: str) -> dict[str, Any] | None:
    for observation in request.observations:
        if observation.tool == tool and observation.result is not None:
            return observation.result
    return None
