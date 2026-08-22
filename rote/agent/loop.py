from __future__ import annotations

import time
from typing import Any

from rote.contracts.agent import (
    AgentBudget,
    AgentDecision,
    DecisionRequest,
    LanguageModel,
    Observation,
)
from rote.contracts.common import Domain, UntrustedText
from rote.contracts.errors import AgentProtocolError, PolicyError, RoteError
from rote.contracts.tools import Toolbox
from rote.contracts.trajectory import GateVerdict, Trajectory
from rote.recorder.recorder import TrajectoryRecorder


def run_agent(
    *,
    domain: Domain,
    task_input: dict[str, Any],
    untrusted: tuple[UntrustedText, ...],
    toolbox: Toolbox,
    model: LanguageModel,
    recorder: TrajectoryRecorder,
    budget: AgentBudget,
    correlation_id: str,
    dry_run: bool = True,
) -> Trajectory:
    specs = toolbox.available_tools()
    offered = tuple(spec.name for spec in specs)
    recorder.start(
        correlation_id=correlation_id,
        domain=domain,
        task_input_redacted=task_input,
        untrusted_text_paths=tuple(block.source_path for block in untrusted),
        agent_model_id=model.model_id,
        prompt_template_id=model.prompt_template_id,
        dry_run=dry_run,
    )

    observations: list[Observation] = []
    errors = 0

    for step_number in range(budget.max_steps):
        response = model.decide(
            DecisionRequest(
                task_input=task_input,
                untrusted=untrusted,
                available_tools=specs,
                observations=tuple(observations),
                steps_remaining=budget.max_steps - step_number,
            )
        )
        recorder.record_usage(response.tokens_in, response.tokens_out)
        decision = response.decision

        if decision.action == "finish":
            return recorder.finish(outcome="resolved")
        if decision.action == "escalate":
            return recorder.finish(outcome="escalated")

        try:
            ensure_tool_is_offered(decision.tool, offered)
        except AgentProtocolError:
            return recorder.finish(outcome="failed")

        observation, verdict = _invoke(toolbox, recorder, decision)
        observations.append(observation)
        # a bounded agent may not route around a refusal, so an escalation ends the run
        if verdict is GateVerdict.ESCALATE:
            return recorder.finish(outcome="escalated")
        if observation.error is not None:
            errors += 1
            if errors >= budget.max_tool_errors:
                return recorder.finish(outcome="escalated")

    return recorder.finish(outcome="escalated")


def ensure_tool_is_offered(tool: str | None, offered: tuple[str, ...]) -> None:
    if tool not in offered:
        raise AgentProtocolError(f"model asked for tool {tool!r}, which was never offered")


def _invoke(
    toolbox: Toolbox, recorder: TrajectoryRecorder, decision: AgentDecision
) -> tuple[Observation, GateVerdict]:
    tool = decision.tool
    arguments = decision.arguments or {}
    assert tool is not None
    permitted = GateVerdict.PERMIT if toolbox.enforces_policy else GateVerdict.UNGATED
    started = time.monotonic()
    try:
        result: dict[str, Any] | None = toolbox.invoke(tool, arguments)
        failure: tuple[str, str] | None = None
        verdict = permitted
    except PolicyError as error:
        result = None
        failure = (type(error).__name__, str(error))
        verdict = error.verdict if isinstance(error.verdict, GateVerdict) else permitted
    except RoteError as error:
        result = None
        failure = (type(error).__name__, str(error))
        verdict = permitted
    latency_ms = int((time.monotonic() - started) * 1000)

    recorder.record_step(
        tool=tool,
        args=arguments,
        result=result,
        error=failure,
        gate_verdict=verdict,
        idempotency_key=_idempotency_key(arguments),
        latency_ms=latency_ms,
    )
    return (
        Observation(
            tool=tool,
            arguments=arguments,
            result=result,
            error=None if failure is None else failure[1],
        ),
        verdict,
    )


def _idempotency_key(arguments: dict[str, Any]) -> str | None:
    key = arguments.get("idempotency_key")
    return key if isinstance(key, str) else None
