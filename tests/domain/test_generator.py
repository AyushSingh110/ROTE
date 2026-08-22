import pytest

from rote.contracts.canonical import canonical_bytes
from rote.contracts.common import GENERATED_CATEGORIES, Domain, ExceptionCategory
from rote.contracts.errors import GeneratorError
from rote.contracts.reconciliation import (
    AdjustmentReason,
    ExpectedEndState,
    GroundTruth,
    SettlementStatus,
)
from rote.domain.generators.reconciliation import INJECTION_MARKERS, generate_dataset
from rote.domain.tools.registry import TOOL_NAMES

SEED = 7
COUNT = 120


def dataset_bytes(seed: int, count: int) -> bytes:
    return canonical_bytes(generate_dataset(seed=seed, count=count).model_dump(mode="json"))


class TestReproducibility:
    def test_the_same_seed_produces_byte_identical_output(self):
        assert dataset_bytes(SEED, COUNT) == dataset_bytes(SEED, COUNT)

    def test_a_different_seed_produces_different_output(self):
        assert dataset_bytes(SEED, COUNT) != dataset_bytes(SEED + 1, COUNT)

    def test_category_order_is_stable_for_a_seed(self):
        first = [gt.category for gt in generate_dataset(seed=SEED, count=COUNT).ground_truths]
        second = [gt.category for gt in generate_dataset(seed=SEED, count=COUNT).ground_truths]
        assert first == second

    def test_the_whole_dataset_is_canonically_serialisable(self):
        assert dataset_bytes(SEED, COUNT)


class TestShape:
    def test_the_requested_number_of_exceptions_is_produced(self):
        assert len(generate_dataset(seed=SEED, count=COUNT).exceptions) == COUNT

    def test_every_exception_has_exactly_one_ground_truth(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        assert [e.exception_id for e in dataset.exceptions] == [
            g.exception_id for g in dataset.ground_truths
        ]

    def test_all_six_approved_categories_are_generated(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        assert {gt.category for gt in dataset.ground_truths} == set(GENERATED_CATEGORIES)

    def test_unknown_is_never_generated(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        assert all(gt.category is not ExceptionCategory.UNKNOWN for gt in dataset.ground_truths)

    def test_every_exception_is_in_the_reconciliation_domain(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        assert all(e.domain is Domain.RECONCILIATION for e in dataset.exceptions)

    def test_exception_ids_are_unique(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        assert len({e.exception_id for e in dataset.exceptions}) == COUNT

    def test_a_non_positive_count_is_rejected(self):
        with pytest.raises(GeneratorError):
            generate_dataset(seed=SEED, count=0)

    def test_a_count_too_small_to_cover_every_category_is_rejected(self):
        with pytest.raises(GeneratorError):
            generate_dataset(seed=SEED, count=3)


class TestGroundTruthDescribesEndStateOnly:
    def test_ground_truth_never_names_a_tool(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        blob = canonical_bytes(
            [gt.model_dump(mode="json") for gt in dataset.ground_truths]
        ).decode()
        for tool_name in TOOL_NAMES:
            assert tool_name not in blob

    def test_ground_truth_carries_no_procedure_shaped_field(self):
        banned = {"step", "steps", "tool", "tools", "sequence", "procedure", "plan", "actions"}
        declared = set(GroundTruth.model_fields) | set(ExpectedEndState.model_fields)
        assert not (declared & banned)

    def test_expected_end_state_only_describes_terminal_facts(self):
        assert set(ExpectedEndState.model_fields) == {
            "settlement_status",
            "matched_bank_line_id",
            "voided_bank_line_id",
            "adjustment_minor_units",
            "adjustment_currency",
            "adjustment_reason",
        }

    def test_expected_end_state_references_only_existing_bank_lines(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        known = {line.line_id for line in dataset.world.bank_lines}
        for truth in dataset.ground_truths:
            end = truth.expected_end_state
            assert end.matched_bank_line_id is None or end.matched_bank_line_id in known
            assert end.voided_bank_line_id is None or end.voided_bank_line_id in known

    def test_an_adjustment_amount_always_comes_with_a_reason_and_currency(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        for truth in dataset.ground_truths:
            end = truth.expected_end_state
            if end.adjustment_minor_units == 0:
                assert end.adjustment_reason is None
            else:
                assert end.adjustment_reason is not None
                assert end.adjustment_currency is not None


class TestTrustedAndUntrustedSeparation:
    def test_structured_facts_contain_no_free_text_content(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        for exception in dataset.exceptions:
            facts = canonical_bytes(exception.facts.model_dump(mode="json")).decode()
            for text in exception.untrusted:
                assert text.content not in facts

    def test_injection_payloads_never_reach_the_structured_facts(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        for exception in dataset.exceptions:
            facts = canonical_bytes(exception.facts.model_dump(mode="json")).decode()
            for marker in INJECTION_MARKERS:
                assert marker not in facts

    def test_some_exceptions_carry_an_injection_payload(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        carriers = [
            exception
            for exception in dataset.exceptions
            if any(m in t.content for t in exception.untrusted for m in INJECTION_MARKERS)
        ]
        assert carriers

    def test_injected_exceptions_keep_their_original_category(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        truths = {gt.exception_id: gt for gt in dataset.ground_truths}
        for exception in dataset.exceptions:
            if any(m in t.content for t in exception.untrusted for m in INJECTION_MARKERS):
                assert truths[exception.exception_id].category in GENERATED_CATEGORIES

    def test_every_untrusted_block_declares_its_source_path_and_length(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        for exception in dataset.exceptions:
            for text in exception.untrusted:
                assert text.source_path.startswith("$.")
                assert text.byte_length == len(text.content.encode())


class TestCategoryArithmetic:
    def test_fee_mismatch_bank_amount_is_internal_minus_the_scheduled_fee(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        truths = {gt.exception_id: gt for gt in dataset.ground_truths}
        lines = {line.line_id: line for line in dataset.world.bank_lines}
        seen = 0
        for exception in dataset.exceptions:
            truth = truths[exception.exception_id]
            if truth.category is not ExceptionCategory.FEE_MISMATCH:
                continue
            seen += 1
            end = truth.expected_end_state
            assert end.matched_bank_line_id is not None
            bank = lines[end.matched_bank_line_id]
            internal = exception.facts.internal_amount.minor_units
            assert bank.amount.minor_units + end.adjustment_minor_units == internal
            assert end.adjustment_reason is AdjustmentReason.FEE
        assert seen

    def test_timing_cutoff_amounts_match_exactly_and_need_no_adjustment(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        truths = {gt.exception_id: gt for gt in dataset.ground_truths}
        lines = {line.line_id: line for line in dataset.world.bank_lines}
        seen = 0
        for exception in dataset.exceptions:
            truth = truths[exception.exception_id]
            if truth.category is not ExceptionCategory.TIMING_CUTOFF:
                continue
            seen += 1
            end = truth.expected_end_state
            assert end.adjustment_minor_units == 0
            assert end.matched_bank_line_id is not None
            bank = lines[end.matched_bank_line_id]
            assert bank.amount == exception.facts.internal_amount
            assert bank.value_date > exception.facts.captured_on
        assert seen

    def test_partial_payment_settles_short_and_stays_partial(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        truths = {gt.exception_id: gt for gt in dataset.ground_truths}
        seen = 0
        for truth in truths.values():
            if truth.category is not ExceptionCategory.PARTIAL_PAYMENT:
                continue
            seen += 1
            end = truth.expected_end_state
            assert end.settlement_status is SettlementStatus.PARTIALLY_SETTLED
            assert end.adjustment_minor_units > 0
            assert end.adjustment_reason is AdjustmentReason.SHORTFALL
        assert seen

    def test_duplicate_entry_voids_exactly_one_line_and_matches_the_other(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        seen = 0
        for truth in generate_dataset(seed=SEED, count=COUNT).ground_truths:
            if truth.category is not ExceptionCategory.DUPLICATE_ENTRY:
                continue
            seen += 1
            end = truth.expected_end_state
            assert end.matched_bank_line_id is not None
            assert end.voided_bank_line_id is not None
            assert end.matched_bank_line_id != end.voided_bank_line_id
        assert seen
        assert dataset.seed == SEED

    def test_transposed_reference_amounts_match_but_references_differ(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        truths = {gt.exception_id: gt for gt in dataset.ground_truths}
        lines = {line.line_id: line for line in dataset.world.bank_lines}
        seen = 0
        for exception in dataset.exceptions:
            truth = truths[exception.exception_id]
            if truth.category is not ExceptionCategory.TRANSPOSED_REFERENCE:
                continue
            seen += 1
            matched = truth.expected_end_state.matched_bank_line_id
            assert matched is not None
            bank = lines[matched]
            assert bank.amount == exception.facts.internal_amount
            assert bank.narration_reference != exception.facts.internal_reference
            assert sorted(bank.narration_reference) == sorted(exception.facts.internal_reference)
        assert seen

    def test_fx_rounding_adjustment_is_small_and_cross_currency(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        truths = {gt.exception_id: gt for gt in dataset.ground_truths}
        lines = {line.line_id: line for line in dataset.world.bank_lines}
        seen = 0
        for exception in dataset.exceptions:
            truth = truths[exception.exception_id]
            if truth.category is not ExceptionCategory.FX_ROUNDING:
                continue
            seen += 1
            end = truth.expected_end_state
            assert end.matched_bank_line_id is not None
            bank = lines[end.matched_bank_line_id]
            assert bank.amount.currency is not exception.facts.internal_amount.currency
            assert 0 < abs(end.adjustment_minor_units) <= 5
            assert end.adjustment_reason is AdjustmentReason.FX_ROUNDING
        assert seen


class TestWorldConsistency:
    def test_every_exception_points_at_a_real_settlement_record(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        known = {record.record_id for record in dataset.world.settlement_records}
        assert all(e.facts.record_id in known for e in dataset.exceptions)

    def test_candidate_bank_lines_all_exist(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        known = {line.line_id for line in dataset.world.bank_lines}
        for exception in dataset.exceptions:
            assert set(exception.facts.candidate_bank_line_ids) <= known

    def test_every_merchant_has_a_fee_schedule(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        scheduled = {schedule.merchant_id for schedule in dataset.world.fee_schedules}
        assert all(e.facts.merchant_id in scheduled for e in dataset.exceptions)

    def test_bank_line_ids_are_unique(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        ids = [line.line_id for line in dataset.world.bank_lines]
        assert len(ids) == len(set(ids))

    def test_all_settlement_records_start_unmatched(self):
        dataset = generate_dataset(seed=SEED, count=COUNT)
        assert all(
            record.status is SettlementStatus.UNMATCHED
            for record in dataset.world.settlement_records
        )

    def test_the_world_starts_with_no_adjustments(self):
        assert generate_dataset(seed=SEED, count=COUNT).world.adjustments == ()
