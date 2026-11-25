"""Prompt files live beside this module; bump PROMPT_VERSION when any of them change."""

import re
from pathlib import Path

PROMPT_VERSION = "1"
PROMPT_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def render_prompt(name: str, **variables: str) -> str:
    """Load prompt ``name`` and substitute ``{{placeholder}}`` values in one pass.

    Unknown placeholders are replaced by an empty string; substituted values are not
    re-scanned, so packet text containing ``{{...}}`` is left untouched.
    """
    template = load_prompt(name)
    return _PLACEHOLDER.sub(lambda m: variables.get(m.group(1), ""), template)
