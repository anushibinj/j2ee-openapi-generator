from __future__ import annotations

import json
import re

import pytest

from j2ee_openapi_generator.llm.schemas import EndpointProposal
from j2ee_openapi_generator.prompts import PROMPT_VERSION, load_prompt, render_prompt

BLOCK = re.compile(r"<!-- example: ([\w-]+) -->\s*```json\n(.*?)\n```", re.S)
EXPECTED = {
    "list-with-paging",
    "read",
    "save-create-or-update",
    "delete-in-use-conflict",
    "wizard-to-draft-resource",
    "export-binary-get",
    "dropdown-lookup",
    "page-only",
}


def test_examples_validate() -> None:
    found = BLOCK.findall(load_prompt("analyzer"))
    assert {n for n, _ in found} == EXPECTED
    for _, text in found:
        EndpointProposal.model_validate(json.loads(text))


@pytest.mark.parametrize("name", ["analyzer", "normalizer", "resolver"])
def test_no_em_dashes_and_placeholders(name: str) -> None:
    text = load_prompt(name)
    assert "—" not in text
    assert "{{style_guide}}" in text and "{{schema}}" in text


def test_analyzer_placeholders_and_render() -> None:
    t = load_prompt("analyzer")
    for ph in ("{{packet}}", "{{feedback}}"):
        assert ph in t
    assert "{{listing}}" in load_prompt("normalizer")
    assert "{{conflicts}}" in load_prompt("resolver")
    out = render_prompt(
        "analyzer", style_guide="SG", schema="SC", packet="0001| {{style_guide}}", feedback="FB"
    )
    assert "{{packet}}" not in out and "0001| {{style_guide}}" in out
    assert PROMPT_VERSION == "1"
