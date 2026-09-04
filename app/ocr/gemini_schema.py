"""Translate the shared RESPONSE_SCHEMA into what Gemini actually accepts.

`app.ocr.prompt.RESPONSE_SCHEMA` is standard JSON Schema, written once and
used by both LLM backends so they cannot drift. Anthropic takes it verbatim.
Gemini does not: `generation_config.response_schema` is a restricted subset
of OpenAPI 3.0, and unsupported keys are rejected outright rather than
ignored.

The trap is that `google.genai.types.Schema` models a SUPERSET of what the
service accepts, so the SDK serialises unsupported keys happily and the
request fails server-side with a message about the snake_case field name it
generated::

    400 INVALID_ARGUMENT ... Unknown name "additional_properties" at
    'generation_config.response_schema': Cannot find field.

`additionalProperties` is in `types.Schema.model_fields` (aliased to
`additionalProperties` on the wire) yet has no counterpart in the server's
proto. So the SDK's own field list cannot be used as the allow-list -- this
module keeps a narrower one, and everything outside it is dropped.

This translation is applied per request, to a copy. The shared schema is
never mutated: `additionalProperties: false` is meaningful to Anthropic's
strict structured-output mode and must stay there.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Keys the Gemini response_schema subset accepts. Deliberately narrower than
# google.genai.types.Schema's fields -- see the module docstring for why that
# list is not usable here. Anything absent is dropped rather than renamed:
# `additionalProperties` has no camelCase-vs-snake_case fix, the service
# simply has no such field.
GEMINI_SCHEMA_KEYS: frozenset[str] = frozenset(
    {
        "type",
        "format",
        "title",
        "description",
        "nullable",
        "enum",
        "default",
        "example",
        "pattern",
        "items",
        "properties",
        "required",
        "anyOf",
        "propertyOrdering",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minProperties",
        "maxProperties",
    }
)

# Dropping one of these does not merely lose a hint, it changes what the
# schema MEANS -- a bare `$ref` node would become an empty `{}` that matches
# anything. The shared schema contains none of them today; if one ever
# appears, fail loudly here rather than silently sending a schema that no
# longer describes the response.
STRUCTURAL_KEYS: frozenset[str] = frozenset(
    {"$ref", "$defs", "definitions", "allOf", "oneOf", "not", "if", "then", "else"}
)

_NULL_BRANCH = {"type": "null"}


class UnsupportedGeminiSchema(ValueError):
    """The schema uses a construct that cannot be expressed to Gemini."""


def _is_null_branch(branch: Any) -> bool:
    return isinstance(branch, dict) and branch.get("type") == "null"


def _collapse_nullable(node: dict[str, Any]) -> dict[str, Any]:
    """Rewrite `anyOf: [X, {"type": "null"}]` as X plus `nullable: true`.

    Gemini's Type enum has no usable NULL member for this position, and
    `nullable` is the documented way to express an optional value. The SDK
    does define Type.NULL, but -- exactly like additionalProperties -- that
    is the SDK's superset, not the service's, so relying on it would swap one
    server-side rejection for another.
    """
    any_of = node.get("anyOf")
    if not isinstance(any_of, list) or not any(_is_null_branch(b) for b in any_of):
        return node

    non_null = [b for b in any_of if not _is_null_branch(b)]
    rest = {k: v for k, v in node.items() if k != "anyOf"}

    if len(non_null) == 1 and isinstance(non_null[0], dict):
        # The common "string or null" shape: merge the single branch up into
        # the parent so `description` on the parent is preserved.
        return {**rest, **non_null[0], "nullable": True}
    if not non_null:
        # anyOf: [null] -- degenerate, nothing left to describe.
        return {**rest, "nullable": True}
    return {**rest, "anyOf": non_null, "nullable": True}


def to_gemini_schema(schema: Any, *, _path: str = "$") -> Any:
    """Return a copy of `schema` containing only what Gemini accepts.

    Recurses through `properties`, `items` and `anyOf`. Raises
    `UnsupportedGeminiSchema` if a structural key is present, since dropping
    one would produce a schema that quietly means something else.
    """
    if isinstance(schema, list):
        return [to_gemini_schema(item, _path=f"{_path}[{i}]") for i, item in enumerate(schema)]
    if not isinstance(schema, dict):
        return schema

    present_structural = STRUCTURAL_KEYS & schema.keys()
    if present_structural:
        raise UnsupportedGeminiSchema(
            f"Gemini's response_schema cannot express {sorted(present_structural)} "
            f"(at {_path}). Inline the construct in app/ocr/prompt.py, or extend "
            "app/ocr/gemini_schema.py to translate it -- silently dropping it would "
            "send Gemini a schema that no longer describes the expected response."
        )

    node = _collapse_nullable(schema)

    cleaned: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in node.items():
        if key not in GEMINI_SCHEMA_KEYS:
            dropped.append(key)
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {
                name: to_gemini_schema(sub, _path=f"{_path}.{name}") for name, sub in value.items()
            }
        elif key in ("items", "anyOf"):
            cleaned[key] = to_gemini_schema(value, _path=f"{_path}.{key}")
        else:
            cleaned[key] = value

    if dropped:
        logger.debug("Dropped unsupported Gemini schema keys at %s: %s", _path, sorted(dropped))
    return cleaned
