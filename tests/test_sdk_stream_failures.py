"""A misconfigured deployment must not be reported as a user error.

When parsec-dev was switched to ``agent.runtime=sdk`` the ``claude`` CLI
subprocess had no credentials of its own, and its complaint reached the chat box
word for word:

    the agent runtime failed: Not logged in · Please run /login

An investigator read that as an instruction and typed ``/login`` into Parsec.
The runtime's wording assumes a terminal; the reader has a chat box, no way to
authenticate anything, and no reason to suspect the deployment. So the classes
of failure that are really deployment faults are named as such, with the raw
text kept alongside for whoever reads the log.
"""

from __future__ import annotations

from typing import Any

from src.agent.sdk_stream import SdkEventTranslator


class _Result:
    """Stand-in for the SDK's ``ResultMessage``."""

    def __init__(self, *, result: str, subtype: str = "error_during_execution") -> None:
        self.is_error = True
        self.subtype = subtype
        self.result = result
        self.num_turns = 1
        self.usage: dict[str, Any] = {}


def _reason(result: str) -> str:
    t = SdkEventTranslator(question="why is ocpv07 down?", history=[])
    t._usage = _Result(result=result)
    return t._failure_reason()


def test_not_logged_in_is_reported_as_a_deployment_fault() -> None:
    reason = _reason("Not logged in · Please run /login")

    assert "could not authenticate" in reason
    assert "deployment problem" in reason
    # The raw text survives for the operator, but attributed to the runtime
    # rather than left standing as an instruction to the reader.
    assert "the runtime said: Not logged in · Please run /login" in reason


def test_invalid_api_key_is_reported_as_a_deployment_fault() -> None:
    assert "could not authenticate" in _reason("API Error: 401 invalid api key")


def test_unknown_model_names_the_gateway_mismatch() -> None:
    """What a wrong anthropic.model against the LiteLLM gateway actually returns."""
    reason = _reason(
        "API Error: 400 {'error': 'anthropic_messages: Invalid model name passed in "
        "model=claude-opus-4-8. Call `/v1/models` to see available models'}"
    )

    assert "does not serve" in reason
    assert "anthropic.model" in reason


def test_unrecognised_failures_are_passed_through_verbatim() -> None:
    """No guessing: anything unmatched keeps the runtime's own words."""
    reason = _reason("Connection reset by peer")

    assert reason == "the agent runtime failed: Connection reset by peer"


def test_turn_limit_is_not_mistaken_for_a_credentials_problem() -> None:
    t = SdkEventTranslator(question="q", history=[])
    t._usage = _Result(result="", subtype="error_max_turns")

    assert "turn limit" in t._failure_reason()


def test_a_successful_run_reports_no_failure() -> None:
    t = SdkEventTranslator(question="q", history=[])
    msg = _Result(result="fine")
    msg.is_error = False
    t._usage = msg
    t._text_parts.append("ocpv07 has 3 nodes NotReady")

    assert t._failure_reason() == ""
