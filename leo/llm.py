"""Claude calls with structured output and a response cache (ROADMAP B3; B7 reuses it).

Why not `client.messages.parse`: in SDK 2.4.0 it validates the JSON inside the call, so a
refusal, a truncated reply or one bad item raises before we can read `stop_reason` or keep the
raw text. `structured_call` asks for the same JSON schema through `output_config.format`, checks
`stop_reason`, and returns the decoded JSON. The caller validates it item by item.

The cache makes reruns reproduce the same output (D5) and lets the demo run with no network
(ROADMAP 3.2). Its key covers the whole request (system prompt, user prompt and schema), so any
change to what is sent misses the cache. Only complete answers (`end_turn` with valid JSON) are
cached, so a failed call is retried on the next run. With `offline=True` a cache miss raises
`LLMOfflineMiss` instead of calling the API.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic

from leo.analytics.usage import record_usage
from pydantic import BaseModel

# --- Schema ----------------------------------------------------------------------------------


def _const_to_enum(node: Any) -> Any:
    """`transform_schema` drops `const` (what pydantic emits for a `Literal`), which would leave
    a term's `type` unconstrained. A one-member `enum` says the same thing and survives."""
    if isinstance(node, dict):
        out = {k: _const_to_enum(v) for k, v in node.items() if k != "const"}
        if "const" in node and "enum" not in node:
            out["enum"] = [node["const"]]
        return out
    if isinstance(node, list):
        return [_const_to_enum(v) for v in node]
    return node


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """The JSON schema to send as `output_config.format.schema` for a pydantic model."""
    return anthropic.transform_schema(_const_to_enum(model.model_json_schema()))


# --- Cache -----------------------------------------------------------------------------------


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def cache_key(
    *, doc_hash: str, stage: str, model: str, prompt_version: str, effort: str, request_hash: str
) -> str:
    """The SHA-256 of what decides the answer. `request_hash` covers the system prompt, the
    user prompt (which holds the document and its class) and the schema, so editing any of them
    misses the cache even if `prompt_version` wasn't bumped. `doc_hash` is in the key too, so
    an entry can be traced to its document."""
    return _sha256(
        _canonical(
            {
                "doc": doc_hash,
                "stage": stage,
                "model": model,
                "prompt_version": prompt_version,
                "effort": effort,
                "request": request_hash,
            }
        )
    )


class ResponseCache:
    """One JSON file per answer, at `<root>/<stage>/<key>.json`. Files are written with sorted
    keys so they diff cleanly in git."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path(self, stage: str, key: str) -> Path:
        return self.root / stage / f"{key}.json"

    def get(self, stage: str, key: str) -> dict[str, Any] | None:
        p = self.path(stage, key)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def put(self, stage: str, key: str, record: dict[str, Any]) -> None:
        p = self.path(stage, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


class LLMOfflineMiss(RuntimeError):
    """Offline mode (`LEO_LLM_OFFLINE=1`) and no cached answer."""


# --- Call ------------------------------------------------------------------------------------


@dataclass
class LLMResult:
    """`output` is the decoded JSON, or None when the call didn't give a complete answer.
    `record` is what gets stored as the item's raw model output: model, prompt version,
    stop reason, usage, request id and output, plus `error` and `text` on failure."""

    output: dict[str, Any] | None
    record: dict[str, Any]
    cached: bool


def structured_call(
    *,
    cache: ResponseCache,
    stage: str,
    doc_hash: str,
    model: str,
    prompt_version: str,
    effort: str,
    system: str,
    user: str,
    schema: dict[str, Any],
    client: anthropic.Anthropic | None = None,
    api_key: str | None = None,
    offline: bool = False,
    max_tokens: int = 16000,
) -> LLMResult:
    """One structured-output request, through the cache. API errors (network, 4xx, 5xx) raise:
    the worker retries or parks the job, and the eval records the error for the document."""
    request_hash = _sha256(_canonical({"system": system, "user": user, "schema": schema}))
    key = cache_key(
        doc_hash=doc_hash,
        stage=stage,
        model=model,
        prompt_version=prompt_version,
        effort=effort,
        request_hash=request_hash,
    )
    hit = cache.get(stage, key)
    if hit is not None:
        return LLMResult(output=hit["output"], record=hit, cached=True)
    if offline:
        raise LLMOfflineMiss(f"no cached {stage} answer for document {doc_hash[:12]} ({key[:12]})")

    # We own the worker retries. New SDK clients must not silently amplify requests.
    client = client or anthropic.Anthropic(api_key=api_key, max_retries=0)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.APIError as exc:
        record_usage(
            "claude", model,
            status="rate_limited" if getattr(exc, "status_code", None) == 429 else "failed",
        )
        raise
    record_usage("claude", response.model, response.usage)
    record: dict[str, Any] = {
        "stage": stage,
        "model": response.model,
        "prompt_version": prompt_version,
        "effort": effort,
        "doc_hash": doc_hash,
        "stop_reason": response.stop_reason,
        "usage": response.usage.model_dump(mode="json", exclude_none=True),
        "request_id": getattr(response, "_request_id", None),
        "output": None,
    }
    text = "".join(b.text for b in response.content if b.type == "text")

    if response.stop_reason != "end_turn":
        details = getattr(response, "stop_details", None)
        record["error"] = f"stop_reason={response.stop_reason}"
        if details is not None:
            record["stop_details"] = details.model_dump(mode="json")
        record["text"] = text
        return LLMResult(output=None, record=record, cached=False)
    try:
        output = json.loads(text)
        if not isinstance(output, dict):
            raise ValueError("top-level JSON is not an object")
    except ValueError as e:
        record["error"] = f"invalid JSON: {e}"
        record["text"] = text
        return LLMResult(output=None, record=record, cached=False)

    record["output"] = output
    cache.put(stage, key, record)
    return LLMResult(output=output, record=record, cached=False)
