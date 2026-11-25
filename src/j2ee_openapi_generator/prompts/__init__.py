"""Prompt files live beside this module; bump PROMPT_VERSION when any of them change."""

from pathlib import Path

PROMPT_VERSION = "1"
PROMPT_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
