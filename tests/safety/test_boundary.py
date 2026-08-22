from typing import Any

import pytest

from rote.contracts.common import Domain
from rote.contracts.errors import BoundaryError
from rote.safety.boundary import ingest
from rote.safety.redaction import REDACTION_MARKERS, redact

RAW: dict[str, Any] = {
    "exception_id": "EXC-000001",
    "record_id": "REC-000001",
    "internal_amount": {"minor_units": 250_000, "currency": "INR"},
    "merchant_note": "Gateway took its usual cut.",
    "bank_narration_text": "NEFT CR SETTLEMENT",
}
FREE_TEXT_PATHS = ("$.merchant_note", "$.bank_narration_text")


def ingested(raw: dict[str, Any] | None = None) -> Any:
    return ingest(
        raw if raw is not None else RAW,
        domain=Domain.RECONCILIATION,
        untrusted_paths=FREE_TEXT_PATHS,
        correlation_id="EXC-000001:run-0",
    )


class TestTheSplitHappensOnce:
    def test_free_text_is_lifted_out_of_the_structured_half(self) -> None:
        task = ingested()
        assert "merchant_note" not in task.structured
        assert "bank_narration_text" not in task.structured

    def test_free_text_arrives_as_labelled_untrusted_blocks(self) -> None:
        task = ingested()
        assert {block.source_path for block in task.untrusted} == set(FREE_TEXT_PATHS)

    def test_the_structured_half_keeps_everything_else(self) -> None:
        task = ingested()
        assert task.structured["record_id"] == "REC-000001"
        assert task.structured["internal_amount"]["minor_units"] == 250_000

    def test_no_untrusted_content_survives_in_the_structured_half(self) -> None:
        task = ingested()
        rendered = str(task.structured)
        for block in task.untrusted:
            assert block.content not in rendered

    def test_a_declared_path_that_is_absent_is_simply_skipped(self) -> None:
        task = ingest(
            {"record_id": "REC-1"},
            domain=Domain.RECONCILIATION,
            untrusted_paths=("$.merchant_note",),
            correlation_id="c-1",
        )
        assert task.untrusted == ()

    def test_a_declared_path_holding_a_non_string_is_refused(self) -> None:
        with pytest.raises(BoundaryError):
            ingest(
                {"merchant_note": {"nested": "object"}},
                domain=Domain.RECONCILIATION,
                untrusted_paths=("$.merchant_note",),
                correlation_id="c-1",
            )

    def test_a_payload_that_cannot_be_canonicalised_is_refused_at_the_edge(self) -> None:
        with pytest.raises(BoundaryError):
            ingest(
                {"rate": 83.25},
                domain=Domain.RECONCILIATION,
                untrusted_paths=(),
                correlation_id="c-1",
            )

    def test_the_correlation_id_is_carried_onto_the_task(self) -> None:
        assert ingested().correlation_id == "EXC-000001:run-0"


class TestRedaction:
    def test_a_card_number_is_removed(self) -> None:
        cleaned, found = redact("charge on 4111111111111111 today")
        assert "4111111111111111" not in cleaned
        assert "card" in found

    def test_an_email_address_is_removed(self) -> None:
        cleaned, found = redact("contact ops@merchant.example")
        assert "ops@merchant.example" not in cleaned
        assert "email" in found

    def test_a_phone_number_is_removed(self) -> None:
        cleaned, found = redact("call +91 98765 43210")
        assert "98765" not in cleaned
        assert "phone" in found

    def test_an_iban_is_removed(self) -> None:
        cleaned, found = redact("to GB33BUKB20201555555555 please")
        assert "GB33BUKB20201555555555" not in cleaned
        assert "iban" in found

    def test_an_ordinary_amount_is_left_alone(self) -> None:
        cleaned, found = redact("adjustment of 31750 paise")
        assert "31750" in cleaned
        assert found == ()

    def test_a_record_id_is_left_alone(self) -> None:
        cleaned, _found = redact("record REC-000001 line BNK-000002")
        assert "REC-000001" in cleaned
        assert "BNK-000002" in cleaned

    def test_what_replaces_a_secret_says_what_kind_it_was(self) -> None:
        cleaned, _found = redact("card 4111111111111111")
        assert REDACTION_MARKERS["card"] in cleaned

    def test_redaction_is_deterministic(self) -> None:
        assert redact("card 4111111111111111") == redact("card 4111111111111111")


class TestRedactionAtIngestion:
    def test_the_structured_half_is_redacted(self) -> None:
        task = ingested({**RAW, "contact": "ops@merchant.example"})
        assert "ops@merchant.example" not in str(task.structured)

    def test_what_was_redacted_is_recorded(self) -> None:
        task = ingested({**RAW, "contact": "ops@merchant.example"})
        assert "email" in task.redactions

    def test_untrusted_text_is_redacted_too(self) -> None:
        task = ingested({**RAW, "merchant_note": "reach me on ops@merchant.example"})
        note = next(b for b in task.untrusted if b.source_path == "$.merchant_note")
        assert "ops@merchant.example" not in note.content

    def test_a_clean_payload_records_no_redactions(self) -> None:
        assert ingested().redactions == ()

    def test_the_declared_byte_length_matches_the_redacted_content(self) -> None:
        task = ingested({**RAW, "merchant_note": "card 4111111111111111 was used"})
        for block in task.untrusted:
            assert block.byte_length == len(block.content.encode())
