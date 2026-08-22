import ast
import inspect
import pathlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from rote.agent.loop import run_agent
from rote.agent.models.offline import OfflineHeuristicModel
from rote.contracts.agent import (
    AgentBudget,
    AgentDecision,
    DecisionRequest,
    LanguageModel,
    ModelResponse,
)
from rote.contracts.errors import AgentProtocolError
from rote.contracts.reconciliation import GeneratedDataset, ReconciliationException
from rote.contracts.tools import Toolbox
from rote.contracts.trajectory import GateVerdict, Trajectory
from rote.domain.generators.reconciliation import generate_dataset
from rote.domain.tools.adapters import ReconciliationTools
from rote.domain.tools.registry import MUTATING_TOOLS, TOOL_NAMES
from rote.recorder.recorder import TrajectoryRecorder

SEED = 5
COUNT = 60

AGENT_PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "rote" / "agent"


def fixed_clock(start: datetime | None = None) -> Iterator[datetime]:
    moment = start or datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)
    while True:
        yield moment
        moment += timedelta(milliseconds=5)


def clock_from(source: Iterator[datetime]) -> Callable[[], datetime]:
    return lambda: next(source)


class ScriptedModel:
    model_id = "scripted-test-model"
    prompt_template_id = "scripted-v1"

    def __init__(self, decisions: list[AgentDecision]) -> None:
        self._decisions = list(decisions)
        self.seen: list[DecisionRequest] = []

    def decide(self, request: DecisionRequest) -> ModelResponse:
        self.seen.append(request)
        decision = (
            self._decisions.pop(0)
            if self._decisions
            else AgentDecision(action="escalate", reason="script exhausted")
        )
        return ModelResponse(decision=decision, tokens_in=11, tokens_out=7)


def build(count: int = COUNT) -> tuple[GeneratedDataset, ReconciliationTools]:
    data = generate_dataset(seed=SEED, count=count)
    return data, ReconciliationTools.from_snapshot(data.world)


def run(
    exception: ReconciliationException,
    tools: ReconciliationTools,
    model: LanguageModel,
    *,
    max_steps: int = 12,
) -> Trajectory:
    return run_agent(
        domain=exception.domain,
        task_input=exception.facts.model_dump(mode="json"),
        untrusted=exception.untrusted,
        toolbox=tools,
        model=model,
        recorder=TrajectoryRecorder(clock=clock_from(fixed_clock())),
        budget=AgentBudget(max_steps=max_steps, max_tool_errors=3),
        correlation_id=f"{exception.exception_id}:run-0",
    )


class TestNoFrameworkAndNoForbiddenKnowledge:
    def test_the_agent_never_imports_langgraph_or_any_framework(self):
        banned = {"langgraph", "langchain", "llama_index", "autogen", "crewai", "haystack"}
        for path in sorted(AGENT_PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert not {a.name.split(".")[0] for a in node.names} & banned
                elif isinstance(node, ast.ImportFrom) and node.module:
                    assert node.module.split(".")[0] not in banned

    def test_the_agent_never_mentions_ground_truth_or_the_test_oracle(self):
        forbidden = ("GroundTruth", "ExpectedEndState", "reference_resolver", "expected_end_state")
        for path in sorted(AGENT_PACKAGE.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for token in forbidden:
                assert token not in source, f"{path.name} mentions {token}"

    def test_the_agent_never_imports_a_tool_adapter(self):
        for path in sorted(AGENT_PACKAGE.rglob("*.py")):
            assert "domain.tools.adapters" not in path.read_text(encoding="utf-8")

    def test_the_run_signature_offers_no_route_to_ground_truth(self):
        rendered = str(inspect.signature(run_agent)).lower()
        for banned in ("truth", "expected", "answer", "checker", "oracle"):
            assert banned not in rendered

    def test_the_reconciliation_tools_satisfy_the_toolbox_protocol(self):
        _data, tools = build(6)
        box: Toolbox = tools
        assert len(box.available_tools()) == len(TOOL_NAMES)


class TestTheLoopTerminates:
    def test_a_model_that_never_finishes_stops_at_the_step_cap(self):
        data, tools = build(6)
        looping = [
            AgentDecision(
                action="call_tool",
                tool="get_settlement_record",
                arguments={"record_id": data.exceptions[0].facts.record_id},
            )
        ] * 50
        trajectory = run(data.exceptions[0], tools, ScriptedModel(looping), max_steps=4)
        assert trajectory.outcome == "escalated"
        assert len(trajectory.steps) == 4

    def test_an_immediate_finish_records_no_steps(self):
        data, tools = build(6)
        trajectory = run(data.exceptions[0], tools, ScriptedModel([AgentDecision(action="finish")]))
        assert trajectory.outcome == "resolved"
        assert trajectory.steps == ()

    def test_an_escalation_is_recorded_as_escalated(self):
        data, tools = build(6)
        trajectory = run(
            data.exceptions[0],
            tools,
            ScriptedModel([AgentDecision(action="escalate", reason="too hard")]),
        )
        assert trajectory.outcome == "escalated"

    def test_repeated_tool_errors_end_the_run(self):
        data, tools = build(6)
        bad = [
            AgentDecision(
                action="call_tool",
                tool="get_settlement_record",
                arguments={"record_id": "REC-999999"},
            )
        ] * 10
        trajectory = run(data.exceptions[0], tools, ScriptedModel(bad))
        assert trajectory.outcome == "escalated"
        assert all(step.error is not None for step in trajectory.steps)
        assert len(trajectory.steps) == 3


class TestMalformedModelOutputFailsLoudly:
    def test_a_tool_the_toolbox_never_offered_is_a_protocol_error(self):
        data, tools = build(6)
        trajectory = run(
            data.exceptions[0],
            tools,
            ScriptedModel(
                [AgentDecision(action="call_tool", tool="wire_money_anywhere", arguments={})]
            ),
        )
        assert trajectory.outcome == "failed"

    def test_a_call_tool_decision_without_a_tool_name_is_rejected(self):
        with pytest.raises(ValidationError):
            AgentDecision(action="call_tool", arguments={})

    def test_a_finish_decision_carrying_a_tool_is_rejected(self):
        with pytest.raises(ValidationError):
            AgentDecision(action="finish", tool="get_settlement_record", arguments={})

    def test_a_protocol_error_names_the_offending_tool(self):
        with pytest.raises(AgentProtocolError) as raised:
            _reject_unknown_tool()
        assert "wire_money_anywhere" in str(raised.value)


def _reject_unknown_tool() -> None:
    from rote.agent.loop import ensure_tool_is_offered

    ensure_tool_is_offered("wire_money_anywhere", ("get_settlement_record",))


class TestWhatTheModelIsAllowedToSee:
    def test_the_model_receives_untrusted_text_in_its_own_labelled_block(self):
        data, tools = build(6)
        model = ScriptedModel([AgentDecision(action="finish")])
        run(data.exceptions[0], tools, model)
        request = model.seen[0]
        assert request.untrusted
        assert all(block.source_path.startswith("$.") for block in request.untrusted)

    def test_the_structured_task_input_carries_no_free_text(self):
        data, tools = build(6)
        model = ScriptedModel([AgentDecision(action="finish")])
        run(data.exceptions[0], tools, model)
        rendered = str(model.seen[0].task_input)
        for block in model.seen[0].untrusted:
            assert block.content not in rendered

    def test_the_model_is_told_how_many_steps_remain(self):
        data, tools = build(6)
        model = ScriptedModel([AgentDecision(action="finish")])
        run(data.exceptions[0], tools, model, max_steps=9)
        assert model.seen[0].steps_remaining == 9

    def test_the_model_only_sees_tools_the_toolbox_offered(self):
        data, tools = build(6)
        model = ScriptedModel([AgentDecision(action="finish")])
        run(data.exceptions[0], tools, model)
        assert {spec.name for spec in model.seen[0].available_tools} == set(TOOL_NAMES)

    def test_mutating_tools_are_flagged_to_the_model(self):
        data, tools = build(6)
        model = ScriptedModel([AgentDecision(action="finish")])
        run(data.exceptions[0], tools, model)
        flagged = {spec.name for spec in model.seen[0].available_tools if spec.mutating}
        assert flagged == set(MUTATING_TOOLS)

    def test_observations_accumulate_across_steps(self):
        data, tools = build(6)
        model = ScriptedModel(
            [
                AgentDecision(
                    action="call_tool",
                    tool="get_settlement_record",
                    arguments={"record_id": data.exceptions[0].facts.record_id},
                ),
                AgentDecision(action="finish"),
            ]
        )
        run(data.exceptions[0], tools, model)
        assert len(model.seen[0].observations) == 0
        assert len(model.seen[1].observations) == 1
        assert model.seen[1].observations[0].tool == "get_settlement_record"


class TestRecordedTrajectory:
    def test_every_step_is_recorded_with_a_fingerprint(self):
        data, tools = build(6)
        model = ScriptedModel(
            [
                AgentDecision(
                    action="call_tool",
                    tool="get_settlement_record",
                    arguments={"record_id": data.exceptions[0].facts.record_id},
                ),
                AgentDecision(action="finish"),
            ]
        )
        trajectory = run(data.exceptions[0], tools, model)
        assert len(trajectory.steps) == 1
        assert len(trajectory.steps[0].result_fingerprint) == 64

    def test_steps_without_a_gate_are_recorded_as_ungated(self):
        data, tools = build(6)
        model = ScriptedModel(
            [
                AgentDecision(
                    action="call_tool",
                    tool="get_settlement_record",
                    arguments={"record_id": data.exceptions[0].facts.record_id},
                ),
                AgentDecision(action="finish"),
            ]
        )
        trajectory = run(data.exceptions[0], tools, model)
        assert trajectory.steps[0].gate_verdict is GateVerdict.UNGATED

    def test_the_model_identity_is_recorded(self):
        data, tools = build(6)
        trajectory = run(data.exceptions[0], tools, ScriptedModel([AgentDecision(action="finish")]))
        assert trajectory.agent_model_id == "scripted-test-model"
        assert trajectory.prompt_template_id == "scripted-v1"

    def test_token_usage_is_accumulated(self):
        data, tools = build(6)
        model = ScriptedModel(
            [
                AgentDecision(
                    action="call_tool",
                    tool="get_settlement_record",
                    arguments={"record_id": data.exceptions[0].facts.record_id},
                ),
                AgentDecision(action="finish"),
            ]
        )
        trajectory = run(data.exceptions[0], tools, model)
        assert trajectory.tokens_in == 22
        assert trajectory.tokens_out == 14

    def test_the_untrusted_paths_are_recorded(self):
        data, tools = build(6)
        trajectory = run(data.exceptions[0], tools, ScriptedModel([AgentDecision(action="finish")]))
        assert trajectory.untrusted_text_paths == ("$.merchant_note", "$.bank_narration_text")

    def test_an_idempotency_key_is_recorded_for_mutating_calls_only(self):
        data, tools = build(6)
        exception = data.exceptions[0]
        model = ScriptedModel(
            [
                AgentDecision(
                    action="call_tool",
                    tool="get_settlement_record",
                    arguments={"record_id": exception.facts.record_id},
                ),
                AgentDecision(
                    action="call_tool",
                    tool="post_adjustment",
                    arguments={
                        "record_id": exception.facts.record_id,
                        "minor_units": 100,
                        "currency": "INR",
                        "reason": "fee",
                        "idempotency_key": "k-1",
                    },
                ),
                AgentDecision(action="finish"),
            ]
        )
        trajectory = run(exception, tools, model)
        assert trajectory.steps[0].idempotency_key is None
        assert trajectory.steps[1].idempotency_key == "k-1"


class TestOfflineModelIsSelfContained:
    def test_the_whole_run_needs_no_api_key_and_no_network(self):
        data, tools = build(6)
        model = OfflineHeuristicModel(seed=1)
        trajectory = run(data.exceptions[0], tools, model, max_steps=12)
        assert trajectory.agent_model_id == "offline-heuristic-1"

    def test_the_offline_model_reaches_a_terminal_outcome(self):
        data, tools = build(COUNT)
        outcomes = {
            run(exception, tools, OfflineHeuristicModel(seed=1)).outcome
            for exception in data.exceptions
        }
        assert outcomes <= {"resolved", "escalated", "failed"}

    def test_the_same_seed_produces_the_same_tool_sequence(self):
        data, tools_one = build(COUNT)
        _data, tools_two = build(COUNT)
        first = [
            [step.tool for step in run(e, tools_one, OfflineHeuristicModel(seed=3)).steps]
            for e in data.exceptions
        ]
        second = [
            [step.tool for step in run(e, tools_two, OfflineHeuristicModel(seed=3)).steps]
            for e in data.exceptions
        ]
        assert first == second

    def test_exploration_changes_the_tool_sequence(self):
        data, tools_one = build(COUNT)
        _data, tools_two = build(COUNT)
        quiet = [
            [step.tool for step in run(e, tools_one, OfflineHeuristicModel(seed=3)).steps]
            for e in data.exceptions
        ]
        noisy = [
            [
                step.tool
                for step in run(e, tools_two, OfflineHeuristicModel(seed=3, exploration=0.5)).steps
            ]
            for e in data.exceptions
        ]
        assert quiet != noisy
