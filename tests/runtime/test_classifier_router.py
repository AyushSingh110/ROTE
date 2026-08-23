import ast
import pathlib
from typing import Any

import pytest

from rote.contracts.classifier import (
    Classification,
    ClassificationRequest,
    ClassificationResponse,
)
from rote.contracts.common import (
    GENERATED_CATEGORIES,
    Domain,
    ExceptionCategory,
    UntrustedText,
)
from rote.contracts.errors import ClassifierError
from rote.contracts.execution import ExecutionState, Handover
from rote.contracts.plan import Plan, PlanStatus
from rote.contracts.routing import RouteKind, RouteReason
from rote.domain.generators.reconciliation import INJECTION_SENTENCES, generate_dataset
from rote.runtime.classifier import Classifier
from rote.runtime.handover import build_handoff
from rote.runtime.preconditions import precondition_holds
from rote.runtime.router import Router

RUNTIME_PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "rote" / "runtime"
DATA = generate_dataset(seed=41, count=120)
TRUTHS = {t.exception_id: t for t in DATA.ground_truths}


class StubModel:
    model_id = "stub-classifier"
    prompt_template_id = "stub-v1"

    def __init__(self, response: ClassificationResponse, is_local: bool = True) -> None:
        self._response = response
        self.is_local = is_local
        self.seen: list[ClassificationRequest] = []

    def classify(self, request: ClassificationRequest) -> ClassificationResponse:
        self.seen.append(request)
        return self._response


def answering(category: str, confidence: int = 900, is_local: bool = True) -> StubModel:
    return StubModel(
        ClassificationResponse(category=category, confidence_per_mille=confidence),
        is_local=is_local,
    )


def facts_for(category: ExceptionCategory) -> dict[str, Any]:
    for exception in DATA.exceptions:
        if TRUTHS[exception.exception_id].category is category:
            return exception.facts.model_dump(mode="json")
    raise AssertionError(f"no {category} in the sample")


def untrusted_for(category: ExceptionCategory) -> tuple[UntrustedText, ...]:
    for exception in DATA.exceptions:
        if TRUTHS[exception.exception_id].category is category:
            return exception.untrusted
    raise AssertionError(f"no {category} in the sample")


class FakeRegistry:
    def __init__(self, plans: dict[ExceptionCategory, Plan]) -> None:
        self._plans = plans

    def active_for(self, domain: Domain, category: ExceptionCategory) -> Plan | None:
        del domain
        return self._plans.get(category)


def a_plan(category: ExceptionCategory) -> Plan:
    from tests.runtime.test_executor import active_plan

    return active_plan().model_copy(
        update={"category": category, "plan_id": f"reconciliation:{category.value}"}
    )


def router_with(*categories: ExceptionCategory, min_confidence: int = 600) -> Router:
    return Router(
        plans=FakeRegistry({category: a_plan(category) for category in categories}),
        domain=Domain.RECONCILIATION,
        min_confidence_per_mille=min_confidence,
    )


class TestTheClassifierReturnsACategoryAndNothingElse:
    def test_a_valid_answer_becomes_a_typed_category(self) -> None:
        classifier = Classifier(model=answering("fee_mismatch"))
        result = classifier.classify(facts_for(ExceptionCategory.FEE_MISMATCH), ())
        assert result.category is ExceptionCategory.FEE_MISMATCH
        assert isinstance(result, Classification)

    def test_the_classification_carries_no_field_a_model_could_act_through(self) -> None:
        declared = set(Classification.model_fields)
        for banned in ("tool", "tools", "action", "args", "arguments", "plan", "steps"):
            assert banned not in declared

    def test_a_category_outside_the_allowed_set_becomes_unknown(self) -> None:
        classifier = Classifier(model=answering("wire_all_the_money"))
        result = classifier.classify(facts_for(ExceptionCategory.FEE_MISMATCH), ())
        assert result.category is ExceptionCategory.UNKNOWN
        assert result.confidence_per_mille == 0

    def test_a_rejected_answer_is_recorded_rather_than_silently_dropped(self) -> None:
        classifier = Classifier(model=answering("post_adjustment"))
        result = classifier.classify(facts_for(ExceptionCategory.FEE_MISMATCH), ())
        assert result.rejected_output == "post_adjustment"

    def test_a_tool_name_offered_as_a_category_never_becomes_an_action(self) -> None:
        classifier = Classifier(model=answering("post_adjustment"))
        result = classifier.classify(facts_for(ExceptionCategory.FEE_MISMATCH), ())
        assert result.category is ExceptionCategory.UNKNOWN

    def test_a_structurally_malformed_answer_raises_loudly(self) -> None:
        class Broken:
            model_id = "broken"
            prompt_template_id = "broken-v1"
            is_local = True

            def classify(self, request: ClassificationRequest) -> ClassificationResponse:
                del request
                return "not a response"  # type: ignore[return-value]

        with pytest.raises(ClassifierError):
            Classifier(model=Broken()).classify(facts_for(ExceptionCategory.FEE_MISMATCH), ())

    def test_unknown_is_never_offered_to_the_model_as_a_choice(self) -> None:
        model = answering("fee_mismatch")
        Classifier(model=model).classify(facts_for(ExceptionCategory.FEE_MISMATCH), ())
        assert ExceptionCategory.UNKNOWN not in model.seen[0].allowed_categories
        assert set(model.seen[0].allowed_categories) == set(GENERATED_CATEGORIES)

    def test_the_model_identity_is_recorded_on_every_classification(self) -> None:
        result = Classifier(model=answering("fee_mismatch")).classify(
            facts_for(ExceptionCategory.FEE_MISMATCH), ()
        )
        assert result.model_id == "stub-classifier"
        assert result.prompt_template_id == "stub-v1"


class TestHostileFreeText:
    def test_every_injected_note_still_yields_a_typed_category(self) -> None:
        classifier = Classifier(model=answering("fee_mismatch"))
        for sentence in INJECTION_SENTENCES:
            note = (UntrustedText.of("$.merchant_note", sentence),)
            result = classifier.classify(facts_for(ExceptionCategory.FEE_MISMATCH), note)
            assert result.category in set(ExceptionCategory)

    def test_a_hostile_note_reaches_the_model_in_its_own_channel(self) -> None:
        model = answering("fee_mismatch")
        note = (UntrustedText.of("$.merchant_note", INJECTION_SENTENCES[0]),)
        Classifier(model=model).classify(facts_for(ExceptionCategory.FEE_MISMATCH), note)
        request = model.seen[0]
        assert request.untrusted == note
        assert INJECTION_SENTENCES[0] not in str(request.task_input)

    def test_free_text_never_reaches_a_hosted_model(self) -> None:
        hosted = answering("fee_mismatch", is_local=False)
        note = (UntrustedText.of("$.merchant_note", "anything at all"),)
        with pytest.raises(ClassifierError):
            Classifier(model=hosted).classify(facts_for(ExceptionCategory.FEE_MISMATCH), note)

    def test_a_hosted_model_may_still_see_structured_fields(self) -> None:
        hosted = answering("fee_mismatch", is_local=False)
        result = Classifier(model=hosted).classify(facts_for(ExceptionCategory.FEE_MISMATCH), ())
        assert result.category is ExceptionCategory.FEE_MISMATCH

    def test_the_classifier_holds_no_tools_at_all(self) -> None:
        classifier = Classifier(model=answering("fee_mismatch"))
        assert not hasattr(classifier, "invoke")
        assert not hasattr(classifier, "available_tools")

    def test_the_classifier_imports_no_adapter_and_no_gate(self) -> None:
        source = (RUNTIME_PACKAGE / "classifier.py").read_text(encoding="utf-8")
        assert "domain.tools" not in source
        assert "safety.gate" not in source


class TestCategoryPreconditions:
    def test_every_generated_category_supports_its_own_facts(self) -> None:
        for category in GENERATED_CATEGORIES:
            assert precondition_holds(category, facts_for(category)) is True

    def test_a_cross_currency_case_contradicts_a_same_currency_label(self) -> None:
        assert (
            precondition_holds(
                ExceptionCategory.FEE_MISMATCH, facts_for(ExceptionCategory.FX_ROUNDING)
            )
            is False
        )

    def test_a_matching_amount_contradicts_a_shortfall_label(self) -> None:
        assert (
            precondition_holds(
                ExceptionCategory.PARTIAL_PAYMENT, facts_for(ExceptionCategory.TIMING_CUTOFF)
            )
            is False
        )

    def test_a_single_candidate_line_contradicts_a_duplicate_label(self) -> None:
        assert (
            precondition_holds(
                ExceptionCategory.DUPLICATE_ENTRY, facts_for(ExceptionCategory.TIMING_CUTOFF)
            )
            is False
        )

    def test_an_unknown_label_never_has_a_precondition(self) -> None:
        assert (
            precondition_holds(ExceptionCategory.UNKNOWN, facts_for(ExceptionCategory.FEE_MISMATCH))
            is False
        )

    def test_missing_structured_fields_fail_the_check_rather_than_pass_it(self) -> None:
        assert precondition_holds(ExceptionCategory.FEE_MISMATCH, {}) is False

    def test_preconditions_are_necessary_not_sufficient(self) -> None:
        # fee and partial payment look identical from the structured side, and that is fine:
        # the check exists to catch a contradiction, not to do the classifying
        fee_facts = facts_for(ExceptionCategory.FEE_MISMATCH)
        assert precondition_holds(ExceptionCategory.PARTIAL_PAYMENT, fee_facts) is True


class TestTheRouterDoesNoReasoning:
    def test_a_confident_supported_category_with_a_plan_routes_to_it(self) -> None:
        # fx_rounding is the unambiguous fixture: exactly one precondition holds on its facts
        router = router_with(ExceptionCategory.FX_ROUNDING)
        route = router.route(
            facts_for(ExceptionCategory.FX_ROUNDING),
            Classification(
                category=ExceptionCategory.FX_ROUNDING,
                confidence_per_mille=900,
                model_id="m",
                prompt_template_id="p",
            ),
        )
        assert route.kind is RouteKind.COMPILED_PLAN
        assert route.reason is RouteReason.PLAN_MATCHED

    def test_an_unknown_category_goes_to_the_live_agent(self) -> None:
        route = router_with(ExceptionCategory.FEE_MISMATCH).route(
            facts_for(ExceptionCategory.FEE_MISMATCH),
            Classification(
                category=ExceptionCategory.UNKNOWN,
                confidence_per_mille=900,
                model_id="m",
                prompt_template_id="p",
            ),
        )
        assert route.kind is RouteKind.LIVE_AGENT
        assert route.reason is RouteReason.UNKNOWN_CATEGORY

    def test_low_confidence_goes_to_the_live_agent(self) -> None:
        route = router_with(ExceptionCategory.FEE_MISMATCH).route(
            facts_for(ExceptionCategory.FEE_MISMATCH),
            Classification(
                category=ExceptionCategory.FEE_MISMATCH,
                confidence_per_mille=100,
                model_id="m",
                prompt_template_id="p",
            ),
        )
        assert route.reason is RouteReason.LOW_CONFIDENCE

    def test_structured_data_contradicting_the_label_goes_to_the_live_agent(self) -> None:
        route = router_with(ExceptionCategory.FEE_MISMATCH).route(
            facts_for(ExceptionCategory.FX_ROUNDING),
            Classification(
                category=ExceptionCategory.FEE_MISMATCH,
                confidence_per_mille=1000,
                model_id="m",
                prompt_template_id="p",
            ),
        )
        assert route.kind is RouteKind.LIVE_AGENT
        assert route.reason is RouteReason.PRECONDITION_CONTRADICTION

    def test_a_category_with_no_active_plan_goes_to_the_live_agent(self) -> None:
        route = router_with().route(
            facts_for(ExceptionCategory.FX_ROUNDING),
            Classification(
                category=ExceptionCategory.FX_ROUNDING,
                confidence_per_mille=900,
                model_id="m",
                prompt_template_id="p",
            ),
        )
        assert route.reason is RouteReason.NO_ACTIVE_PLAN

    def test_a_plan_that_is_not_active_is_never_served(self) -> None:
        shadowing = a_plan(ExceptionCategory.FX_ROUNDING).model_copy(
            update={"status": PlanStatus.SHADOW}
        )
        router = Router(
            plans=FakeRegistry({ExceptionCategory.FX_ROUNDING: shadowing}),
            domain=Domain.RECONCILIATION,
            min_confidence_per_mille=600,
        )
        route = router.route(
            facts_for(ExceptionCategory.FX_ROUNDING),
            Classification(
                category=ExceptionCategory.FX_ROUNDING,
                confidence_per_mille=900,
                model_id="m",
                prompt_template_id="p",
            ),
        )
        assert route.kind is RouteKind.LIVE_AGENT

    def test_the_router_imports_no_model_and_no_embedding(self) -> None:
        banned = {
            "openai",
            "anthropic",
            "groq",
            "ollama",
            "langchain",
            "sklearn",
            "torch",
            "sentence_transformers",
            "numpy",
        }
        tree = ast.parse((RUNTIME_PACKAGE / "router.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in banned
            elif isinstance(node, ast.Import):
                assert not {a.name.split(".")[0] for a in node.names} & banned

    def test_the_router_never_reads_untrusted_text(self) -> None:
        source = (RUNTIME_PACKAGE / "router.py").read_text(encoding="utf-8")
        assert "untrusted" not in source

    def test_routing_is_deterministic(self) -> None:
        router = router_with(ExceptionCategory.FEE_MISMATCH)
        classification = Classification(
            category=ExceptionCategory.FEE_MISMATCH,
            confidence_per_mille=900,
            model_id="m",
            prompt_template_id="p",
        )
        facts = facts_for(ExceptionCategory.FEE_MISMATCH)
        assert router.route(facts, classification) == router.route(facts, classification)


class TestHandover:
    def _handover(self) -> Handover:
        from rote.runtime.executor import execute_plan
        from tests.runtime.test_executor import AlwaysReject, active_plan, toolbox_for

        result = execute_plan(
            plan=active_plan(),
            task_input={"record_id": "REC-99"},
            toolbox=toolbox_for(index=99),
            inspector=AlwaysReject(),
        )
        assert result.handover is not None
        return result.handover

    def test_the_diverging_result_arrives_as_untrusted_text(self) -> None:
        handoff = build_handoff(self._handover(), original_untrusted=())
        paths = {block.source_path for block in handoff.untrusted}
        assert "$.diverging_tool_result" in paths

    def test_the_diverging_result_never_becomes_structured_input(self) -> None:
        handover = self._handover()
        handoff = build_handoff(handover, original_untrusted=())
        assert "BNK-99" not in str(handoff.task_input)

    def test_the_original_untrusted_text_is_carried_across(self) -> None:
        original = untrusted_for(ExceptionCategory.FEE_MISMATCH)
        handoff = build_handoff(self._handover(), original_untrusted=original)
        for block in original:
            assert block in handoff.untrusted

    def test_the_state_the_run_had_reached_is_carried_across(self) -> None:
        handoff = build_handoff(self._handover(), original_untrusted=())
        assert handoff.task_input == {"record_id": "REC-99"}

    def test_the_step_it_stopped_at_is_recorded(self) -> None:
        handoff = build_handoff(self._handover(), original_untrusted=())
        assert handoff.resumed_from_step == 0

    def test_the_handoff_is_canonically_serialisable(self) -> None:
        from rote.contracts.canonical import canonical_bytes

        handoff = build_handoff(self._handover(), original_untrusted=())
        assert canonical_bytes(handoff.model_dump(mode="json"))

    def test_a_handover_with_no_diverging_result_still_works(self) -> None:
        handover = Handover(
            step_index=2,
            reason="tool_error",
            state=ExecutionState(task_input={"record_id": "REC-1"}, committed=()),
            untrusted_result=None,
        )
        handoff = build_handoff(handover, original_untrusted=())
        assert handoff.untrusted == ()
        assert handoff.resumed_from_step == 2


class SpyRegistry:
    def __init__(self, plans: dict[ExceptionCategory, Plan]) -> None:
        self._plans = plans
        self.lookups: list[ExceptionCategory] = []

    def active_for(self, domain: Domain, category: ExceptionCategory) -> Plan | None:
        del domain
        self.lookups.append(category)
        return self._plans.get(category)


def classified(category: ExceptionCategory, confidence: int = 900) -> Classification:
    return Classification(
        category=category,
        confidence_per_mille=confidence,
        model_id="m",
        prompt_template_id="p",
    )


def holding_count(category: ExceptionCategory) -> int:
    facts = facts_for(category)
    return sum(1 for other in GENERATED_CATEGORIES if precondition_holds(other, facts))


class TestAmbiguousEvidenceIsRefused:
    # the whole point of the rule: it is generic, not a special case for the pair that failed
    def test_the_rule_names_no_category_at_all(self) -> None:
        source = (RUNTIME_PACKAGE / "router.py").read_text(encoding="utf-8")
        for category in GENERATED_CATEGORIES:
            assert category.value not in source

    def test_exactly_one_matching_category_still_reaches_the_plan(self) -> None:
        assert holding_count(ExceptionCategory.FX_ROUNDING) == 1
        route = router_with(ExceptionCategory.FX_ROUNDING).route(
            facts_for(ExceptionCategory.FX_ROUNDING), classified(ExceptionCategory.FX_ROUNDING)
        )
        assert route.kind is RouteKind.COMPILED_PLAN
        assert route.reason is RouteReason.PLAN_MATCHED

    @pytest.mark.parametrize(
        "category",
        [
            ExceptionCategory.FEE_MISMATCH,
            ExceptionCategory.PARTIAL_PAYMENT,
            ExceptionCategory.TRANSPOSED_REFERENCE,
            ExceptionCategory.DUPLICATE_ENTRY,
        ],
    )
    def test_two_matching_categories_go_to_the_live_agent(
        self, category: ExceptionCategory
    ) -> None:
        assert holding_count(category) > 1
        route = router_with(category).route(facts_for(category), classified(category))
        assert route.kind is RouteKind.LIVE_AGENT
        assert route.reason is RouteReason.AMBIGUOUS_EVIDENCE

    def test_zero_matching_categories_keeps_the_contradiction_reason(self) -> None:
        route = router_with(ExceptionCategory.FEE_MISMATCH).route(
            {}, classified(ExceptionCategory.FEE_MISMATCH)
        )
        assert route.reason is RouteReason.PRECONDITION_CONTRADICTION

    def test_the_co_holding_categories_are_recorded_sorted(self) -> None:
        route = router_with(ExceptionCategory.FEE_MISMATCH).route(
            facts_for(ExceptionCategory.FEE_MISMATCH),
            classified(ExceptionCategory.FEE_MISMATCH),
        )
        named = [part.strip() for part in route.detail.split(",")]
        assert named == sorted(named)
        assert named == ["fee_mismatch", "partial_payment"]

    # contradiction is the injection defence and must keep priority over ambiguity
    def test_ambiguity_is_checked_after_contradiction(self) -> None:
        route = router_with(ExceptionCategory.FEE_MISMATCH).route(
            facts_for(ExceptionCategory.FX_ROUNDING),
            classified(ExceptionCategory.FEE_MISMATCH),
        )
        assert route.reason is RouteReason.PRECONDITION_CONTRADICTION

    def test_a_missing_plan_cannot_override_ambiguity(self) -> None:
        route = router_with().route(
            facts_for(ExceptionCategory.FEE_MISMATCH),
            classified(ExceptionCategory.FEE_MISMATCH),
        )
        assert route.reason is RouteReason.AMBIGUOUS_EVIDENCE

    def test_an_ambiguous_case_never_carries_a_plan_to_execute(self) -> None:
        route = router_with(ExceptionCategory.FEE_MISMATCH).route(
            facts_for(ExceptionCategory.FEE_MISMATCH),
            classified(ExceptionCategory.FEE_MISMATCH),
        )
        assert route.plan_id is None
        assert route.plan_version is None
        assert route.kind is RouteKind.LIVE_AGENT

    # decided before the registry is consulted, so an ambiguous case cannot reach a plan at all
    def test_an_ambiguous_case_never_consults_the_plan_source(self) -> None:
        category = ExceptionCategory.FEE_MISMATCH
        registry = SpyRegistry({category: a_plan(category)})
        router = Router(plans=registry, domain=Domain.RECONCILIATION, min_confidence_per_mille=600)
        router.route(
            facts_for(ExceptionCategory.FEE_MISMATCH),
            classified(ExceptionCategory.FEE_MISMATCH),
        )
        assert registry.lookups == []

    def test_an_unambiguous_case_does_consult_the_plan_source(self) -> None:
        category = ExceptionCategory.FX_ROUNDING
        registry = SpyRegistry({category: a_plan(category)})
        router = Router(plans=registry, domain=Domain.RECONCILIATION, min_confidence_per_mille=600)
        router.route(
            facts_for(ExceptionCategory.FX_ROUNDING), classified(ExceptionCategory.FX_ROUNDING)
        )
        assert registry.lookups == [ExceptionCategory.FX_ROUNDING]

    def test_ambiguous_routing_is_deterministic(self) -> None:
        router = router_with(ExceptionCategory.FEE_MISMATCH)
        facts = facts_for(ExceptionCategory.FEE_MISMATCH)
        classification = classified(ExceptionCategory.FEE_MISMATCH)
        routes = [router.route(facts, classification) for _ in range(5)]
        assert all(route == routes[0] for route in routes)

    def test_low_confidence_still_outranks_ambiguity(self) -> None:
        route = router_with(ExceptionCategory.FEE_MISMATCH).route(
            facts_for(ExceptionCategory.FEE_MISMATCH),
            classified(ExceptionCategory.FEE_MISMATCH, confidence=100),
        )
        assert route.reason is RouteReason.LOW_CONFIDENCE

    def test_an_unknown_category_still_outranks_ambiguity(self) -> None:
        route = router_with(ExceptionCategory.FEE_MISMATCH).route(
            facts_for(ExceptionCategory.FEE_MISMATCH),
            classified(ExceptionCategory.UNKNOWN),
        )
        assert route.reason is RouteReason.UNKNOWN_CATEGORY
