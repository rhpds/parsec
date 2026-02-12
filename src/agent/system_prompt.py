"""System prompt loader — reads from config/system_prompt.md."""

import os

_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "config",
    "agent_instructions.md",
)


def get_system_prompt() -> str:
    """Load the system prompt from the markdown file."""
    with open(_PROMPT_PATH) as f:
        return f.read()


# For backward compatibility
SYSTEM_PROMPT = get_system_prompt()
