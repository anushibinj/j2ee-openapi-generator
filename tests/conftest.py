"""Shared fixture paths."""

from pathlib import Path

import pytest

ROOT = Path(__file__).parent
FIXTURES = ROOT / "fixtures"
LEGACY_APP = FIXTURES / "legacy-app"
LLM_FIXTURES = FIXTURES / "llm"
GOLDEN = ROOT / "golden"
PROJECT = ROOT.parent


@pytest.fixture
def legacy_app() -> Path:
    return LEGACY_APP


@pytest.fixture
def llm_fixtures() -> Path:
    return LLM_FIXTURES
