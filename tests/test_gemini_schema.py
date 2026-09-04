"""Gemini's response_schema is a narrow subset of JSON Schema.

The shared RESPONSE_SCHEMA is standard JSON Schema, written once so the two
LLM backends cannot drift. Anthropic takes it verbatim. Gemini rejects
anything outside its subset server-side, and the SDK is no help: it models a
SUPERSET of what the service accepts, so unsupported keys serialise happily
and fail on the wire:

    400 INVALID_ARGUMENT ... Unknown name "additional_properties" at
    'generation_config.response_schema': Cannot find field.

These tests pin the translation, and specifically that it is applied to a
COPY -- `additionalProperties: false` is meaningful to Anthropic's strict
mode and must survive in the shared schema.
"""

from __future__ import annotations

import json

import pytest

from app.ocr.gemini_schema import (
    GEMINI_SCHEMA_KEYS,
    UnsupportedGeminiSchema,
    to_gemini_schema,
)
from app.ocr.prompt import RESPONSE_SCHEMA


def _all_keys(node, acc=None):
    acc = set() if acc is None else acc
    if isinstance(node, dict):
        acc |= set(node.keys())
        for v in node.values():
            _all_keys(v, acc)
    elif isinstance(node, list):
        for v in node:
            _all_keys(v, acc)
    return acc


def _schema_keys(node, acc=None):
    """Keys that are schema keywords, ignoring property NAMES."""
    acc = set() if acc is None else acc
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "properties" and isinstance(v, dict):
                for sub in v.values():
                    _schema_keys(sub, acc)
                acc.add(k)
            else:
                acc.add(k)
                _schema_keys(v, acc)
    elif isinstance(node, list):
        for v in node:
            _schema_keys(v, acc)
    return acc


# --------------------------------------------------------- the actual bug --


def test_additional_properties_is_stripped_everywhere():
    """The reported failure. Renaming to camelCase does not help -- the
    service has no such field under any casing, it must be absent."""
    out = to_gemini_schema(RESPONSE_SCHEMA)
    blob = json.dumps(out)
    assert "additionalProperties" not in blob
    assert "additional_properties" not in blob


def test_shared_schema_is_not_mutated():
    """Anthropic's strict structured-output mode wants
    additionalProperties: false, so the translation must copy, not edit."""
    before = json.dumps(RESPONSE_SCHEMA, sort_keys=True)
    to_gemini_schema(RESPONSE_SCHEMA)
    assert json.dumps(RESPONSE_SCHEMA, sort_keys=True) == before
    assert RESPONSE_SCHEMA["additionalProperties"] is False


def test_only_supported_keywords_survive():
    out = to_gemini_schema(RESPONSE_SCHEMA)
    assert _schema_keys(out) <= GEMINI_SCHEMA_KEYS


# ------------------------------------------- the next failure, pre-empted --


def test_nullable_union_is_collapsed_not_passed_through():
    """`anyOf: [{"string"}, {"null"}]` is how the shared schema says
    "optional". Gemini wants `nullable: true` instead; a null-typed branch
    would have been the next server-side rejection after this fix."""
    out = to_gemini_schema(RESPONSE_SCHEMA)
    opd = out["properties"]["opd_number"]
    assert opd["type"] == "string"
    assert opd["nullable"] is True
    assert "anyOf" not in opd
    assert '"null"' not in json.dumps(out)


def test_collapsing_preserves_the_parent_description():
    """The description lives on the parent, the type on the branch -- a naive
    collapse drops the half that tells the model what the field means."""
    out = to_gemini_schema(RESPONSE_SCHEMA)
    assert "HN Hospital/Clinic" in out["properties"]["opd_number"]["description"]
    assert "animal's name" in out["properties"]["patient_name"]["description"]


def test_required_and_property_set_are_unchanged():
    """Stripping must not quietly change what the model is asked to return."""
    out = to_gemini_schema(RESPONSE_SCHEMA)
    assert out["required"] == RESPONSE_SCHEMA["required"]
    assert set(out["properties"]) == set(RESPONSE_SCHEMA["properties"])
    assert out["properties"]["confidence"]["minimum"] == 0.0
    assert out["properties"]["confidence"]["maximum"] == 1.0


# ------------------------------------------------------- general cleaning --


@pytest.mark.parametrize(
    "junk", ["$schema", "additionalProperties", "unevaluatedProperties", "const", "examples"]
)
def test_unsupported_keys_are_dropped_at_any_depth(junk):
    schema = {
        "type": "object",
        junk: "whatever",
        "properties": {"a": {"type": "string", junk: "whatever"}},
    }
    out = to_gemini_schema(schema)
    assert junk not in out
    assert junk not in out["properties"]["a"]


def test_nested_arrays_and_objects_are_cleaned():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"v": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
                },
            }
        },
    }
    out = to_gemini_schema(schema)
    assert "additionalProperties" not in json.dumps(out)
    inner = out["properties"]["rows"]["items"]["properties"]["v"]
    assert inner == {"type": "string", "nullable": True}


def test_multi_branch_union_keeps_anyof_and_adds_nullable():
    schema = {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "null"}]}
    out = to_gemini_schema(schema)
    assert out["nullable"] is True
    assert out["anyOf"] == [{"type": "string"}, {"type": "number"}]


def test_union_without_a_null_branch_is_left_alone():
    schema = {"anyOf": [{"type": "string"}, {"type": "number"}]}
    assert to_gemini_schema(schema) == schema


# ------------------------------------------------- structural keys shout ---


@pytest.mark.parametrize("key", ["$ref", "$defs", "allOf", "oneOf", "not"])
def test_structural_keys_raise_instead_of_being_silently_dropped(key):
    """Dropping these does not lose a hint, it changes the meaning -- a bare
    $ref node would become {} and match anything."""
    schema = {"type": "object", "properties": {"a": {key: "#/$defs/X"}}}
    with pytest.raises(UnsupportedGeminiSchema) as exc:
        to_gemini_schema(schema)
    assert key in str(exc.value)
    assert "$.a" in str(exc.value)  # says where


def test_current_shared_schema_needs_no_structural_translation():
    """If this ever fails, prompt.py grew a $ref/allOf and the Gemini backend
    needs a real translation, not just key stripping."""
    to_gemini_schema(RESPONSE_SCHEMA)  # must not raise


# ------------------------------------------------ end-to-end through SDK ---


def test_translated_schema_serialises_without_the_rejected_field():
    """The SDK accepts additionalProperties locally -- types.Schema has the
    field -- so this asserts on the serialised payload, which is what the
    service actually sees and rejected."""
    types = pytest.importorskip("google.genai.types")

    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=to_gemini_schema(RESPONSE_SCHEMA),
    )
    wire = json.dumps(cfg.model_dump(mode="json", exclude_none=True, by_alias=True))
    assert "additionalProperties" not in wire
    assert "additional_properties" not in wire
    assert '"null"' not in wire
    assert "nullable" in wire


def test_untranslated_schema_would_still_send_the_bad_field():
    """Proves the fix is load-bearing: without it the payload carries the
    field the service rejects."""
    types = pytest.importorskip("google.genai.types")

    cfg = types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=RESPONSE_SCHEMA
    )
    wire = json.dumps(cfg.model_dump(mode="json", exclude_none=True, by_alias=True))
    assert "additionalProperties" in wire


def test_gemini_backend_uses_the_translated_schema(monkeypatch):
    """The converter must translate at construction, not send the raw one."""
    from app.config import Settings
    from app.ocr.gemini import GeminiOcrConverter

    monkeypatch.setattr(
        "google.genai.Client", lambda **kwargs: object(), raising=False
    )
    converter = GeminiOcrConverter(
        Settings(ocr_backend="gemini", gemini_api_key="test-key", _env_file=None)
    )
    assert "additionalProperties" not in json.dumps(converter._response_schema)
    assert converter._response_schema["properties"]["opd_number"]["nullable"] is True
