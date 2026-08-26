"""ROTE_CLASSIFIER selects the classifier, and a bad selection stops the server."""

from __future__ import annotations

import pytest

from rote.agent.models.language import ANTHROPIC, OLLAMA, ROTE_CLASSIFIER
from rote.contracts.errors import ClassifierError
from rote.service.session import configured_classifier, live_session
from rote.web.app import llm_enabled, verification_enabled


class TestTheSwitchIsReadFromTheEnvironment:
    def test_the_default_is_deterministic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ROTE_CLASSIFIER, raising=False)
        assert llm_enabled() is False

    @pytest.mark.parametrize("value", ["", "deterministic", "DETERMINISTIC", " off "])
    def test_anything_that_is_not_llm_leaves_the_default_alone(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ROTE_CLASSIFIER, value)
        assert llm_enabled() is False

    @pytest.mark.parametrize("value", ["llm", "LLM", " llm "])
    def test_llm_is_recognised_however_it_is_typed(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ROTE_CLASSIFIER, value)
        assert llm_enabled() is True

    def test_it_is_independent_of_the_verification_switch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ROTE_CLASSIFIER, "llm")
        monkeypatch.delenv("ROTE_VERIFY_EVIDENCE", raising=False)
        assert llm_enabled() is True
        assert verification_enabled() is False


class TestAMisconfiguredSelectionFailsClosed:
    def test_a_hosted_provider_without_a_key_refuses_to_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ROTE_CLASSIFIER, "llm")
        monkeypatch.setenv("ROTE_LLM_PROVIDER", ANTHROPIC)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ClassifierError):
            configured_classifier()

    def test_asking_for_a_model_in_deterministic_mode_is_an_error_not_a_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ROTE_CLASSIFIER, "deterministic")
        with pytest.raises(ClassifierError):
            configured_classifier()

    def test_an_unknown_provider_refuses_to_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ROTE_CLASSIFIER, "llm")
        monkeypatch.setenv("ROTE_LLM_PROVIDER", "mystery-corp")
        with pytest.raises(ClassifierError):
            configured_classifier()

    # a local provider builds without a credential, but is still handed no free text
    def test_a_local_provider_builds_and_still_withholds_notes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ROTE_CLASSIFIER, "llm")
        monkeypatch.setenv("ROTE_LLM_PROVIDER", OLLAMA)
        model = configured_classifier()
        assert model.is_local is False


class TestTheDefaultSessionIsUnchanged:
    def test_the_cached_session_still_uses_the_deterministic_classifier(self) -> None:
        assert live_session(False, False).classifier_model_id == "structured-fields-double-1"

    def test_the_deterministic_session_is_local_and_withholds_nothing(self) -> None:
        assert live_session(False, False).classifier_is_local is True
