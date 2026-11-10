"""Asking the model for a script (ROADMAP Phase 4).

One structured call per attempt, through `leo.llm.structured_call` and its cache (stage
`scriptgen`, committed under `data/llm_cache/scriptgen/`), so a rerun gets the same code with
no network. The prompt holds the code contract (`sandbox.CONTRACT`), each example's text and
the LLM's answer for it, and on a retry the earlier attempts with their failures. Examples
come in sha256 order and failure messages carry no timings or addresses, so the same run
builds the same prompt and hits the cache.

Model: `LEO_MODEL_SCRIPTGEN` (Haiku by default, effort medium, like every other stage).
"""

import json
from dataclasses import dataclass
from typing import Any

import anthropic
from pydantic import BaseModel

from leo.config import Settings, get_settings
from leo.llm import LLMResult, ResponseCache, json_schema_for, structured_call
from leo.scriptgen.candidates import Group
from leo.scriptgen.sandbox import CONTRACT

STAGE = "scriptgen"
PROMPT_VERSION = "scriptgen-v1"
EFFORT = "medium"
MAX_ATTEMPTS = 3


class ScriptProposal(BaseModel):
    code: str
    notes: str


SCHEMA = json_schema_for(ScriptProposal)

SYSTEM_PROMPT = """\
You write small, strict Python parsers for one recurring business-email format, so that \
future emails in that format are extracted by code instead of by a language model.

You get several emails in the same format and, for each, the extraction that was verified \
for it. Write one module whose parse() reproduces each verified extraction exactly from the \
email text, and whose matches() accepts this format and nothing else.

Prefer anchored, line-based regular expressions (re_search(r"^Label: (.+)$", body, \
MULTILINE)). Cross-check numbers the template states twice (a line item and its total) and \
raise ValueError when they disagree or a line is missing. Never hard-code a value that \
differs between the examples.

Return the module's full source as `code`, and one or two sentences on what it matches and \
checks as `notes`.\
"""


@dataclass
class Attempt:
    code: str
    failures: list[str]


def _example_json(output: dict[str, Any]) -> str:
    return json.dumps(output, indent=1, sort_keys=True, ensure_ascii=False, default=str)


def user_prompt(group: Group, previous: list[Attempt]) -> str:
    parts = [CONTRACT, f"The format: emails from {group.sender}.\n"]
    for i, ex in enumerate(group.examples, 1):
        parts.append(
            f"### Example {i}\n\n<email>\n{ex.doc.text}\n</email>\n\n"
            f"Expected parse(text) output:\n```json\n{_example_json(ex.expected)}\n```\n"
        )
    for i, a in enumerate(previous, 1):
        failures = "\n".join(f"- {f}" for f in a.failures)
        parts.append(
            f"### Your attempt {i} was rejected\n\n```python\n{a.code}\n```\n\n"
            f"It failed these checks:\n{failures}\n"
        )
    if previous:
        parts.append("Write a corrected module that passes every check.\n")
    return "\n".join(parts)


def propose(
    group: Group,
    previous: list[Attempt],
    cache: ResponseCache,
    settings: Settings | None = None,
    client: anthropic.Anthropic | None = None,
) -> tuple[ScriptProposal | None, LLMResult]:
    """One attempt. The proposal is None when the call gave no complete answer."""
    settings = settings or get_settings()
    result = structured_call(
        cache=cache,
        stage=STAGE,
        doc_hash=group.examples_hash,
        model=settings.leo_model_scriptgen,
        prompt_version=PROMPT_VERSION,
        effort=EFFORT,
        system=SYSTEM_PROMPT,
        user=user_prompt(group, previous),
        schema=SCHEMA,
        client=client,
        api_key=settings.anthropic_api_key or None,
        offline=settings.leo_llm_offline,
    )
    if result.output is None:
        return None, result
    try:
        return ScriptProposal.model_validate(result.output), result
    except ValueError:
        return None, result
