"""Verify aap2_fix uses centralized client factory."""


def test_aap2_fix_no_local_factory():
    """Verify local factory functions are removed from aap2_fix."""
    import src.tools.aap2_fix as mod

    assert not hasattr(
        mod, "_create_anthropic_client"
    ), "_create_anthropic_client should be removed"
    assert not hasattr(mod, "_create_vertex_client"), "_create_vertex_client should be removed"
