"""Verify orchestrator uses centralized client factory."""

from unittest.mock import MagicMock


def test_orchestrator_imports_from_client_factory():
    """Verify _build_client is no longer defined in orchestrator."""
    import src.agent.orchestrator as orch

    assert not hasattr(
        orch, "_build_client"
    ), "_build_client should be removed from orchestrator.py"


def test_parse_response_blocks_strips_thinking_tokens():
    """Verify _parse_response_blocks strips <think> tags from text."""
    from src.agent.orchestrator import _parse_response_blocks

    block = MagicMock()
    block.type = "text"
    block.text = "<think>\nreasoning here\n</think>\nThe answer is 42."

    text_parts, tool_blocks = _parse_response_blocks([block])
    assert text_parts == ["The answer is 42."]
    assert tool_blocks == []


def test_parse_response_blocks_preserves_clean_text():
    """Verify _parse_response_blocks doesn't alter text without <think> tags."""
    from src.agent.orchestrator import _parse_response_blocks

    block = MagicMock()
    block.type = "text"
    block.text = "Normal response text."

    text_parts, _ = _parse_response_blocks([block])
    assert text_parts == ["Normal response text."]
