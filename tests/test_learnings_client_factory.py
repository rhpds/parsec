"""Verify learnings uses centralized client factory."""


def test_learnings_no_local_analyze_functions():
    """Verify backend-specific analyze functions are removed."""
    import src.agent.learnings as mod

    assert not hasattr(mod, "_analyze_direct"), "_analyze_direct should be removed"
    assert not hasattr(mod, "_analyze_vertex"), "_analyze_vertex should be removed"
    assert not hasattr(mod, "_analyze_bedrock"), "_analyze_bedrock should be removed"
