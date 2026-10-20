"""strict_schema: structured-outputs-taugliches JSON-Schema aus Pydantic-Modellen."""

from __future__ import annotations

import json
from typing import Literal

import pytest
from pydantic import BaseModel, Field

from wdvi.brain.claude import _extract_json_object, _parse_json_model, _ParseError, strict_schema
from wdvi.schemas import EditPlan, MomentPicks

UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength",
               "pattern", "default", "title", "examples", "multipleOf", "maxItems", "uniqueItems",
               "prefixItems", "$ref", "$defs", "definitions", "oneOf"}


def _walk(node, path="$"):
    """Alle Schema-Knoten (ohne Property-Namen als Keywords zu verwechseln)."""
    if isinstance(node, dict):
        yield path, node
        for key, val in node.items():
            if key == "properties":
                for name, sub in val.items():
                    yield from _walk(sub, f"{path}.{name}")
            elif isinstance(val, dict):
                yield from _walk(val, f"{path}/{key}")
            elif isinstance(val, list):
                for i, sub in enumerate(val):
                    yield from _walk(sub, f"{path}/{key}[{i}]")


def _assert_strict(schema: dict) -> None:
    assert "$ref" not in json.dumps(schema)
    for path, node in _walk(schema):
        bad = UNSUPPORTED & set(node)
        assert not bad, f"{path}: unsupported keywords {bad}"
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, path
            assert node.get("required") == list(node.get("properties", {})), path
        if "format" in node:
            assert node["format"] in {"date-time", "date", "uri"}, path
        if "minItems" in node:
            assert node["minItems"] in (0, 1), path


@pytest.mark.parametrize("model", [EditPlan, MomentPicks])
def test_strict_schema_is_closed_and_ref_free(model):
    schema = strict_schema(model)
    _assert_strict(schema)
    assert schema["type"] == "object"
    assert set(schema["required"]) == set(model.model_fields)


def test_edit_plan_details():
    s = strict_schema(EditPlan)
    props = s["properties"]
    # Tupel -> array of number, feste Länge steht in der description
    assert props["remove_ranges"] == {"type": "array", "items": {
        "type": "array", "items": {"type": "number"}, "description": "Exactly 2 items: [number, number]"}}
    # deutsche Modell-Docstrings gehören nicht ins Schema
    assert "description" not in s
    assert "Claudes" not in json.dumps(s) and "Ausschnitt" not in json.dumps(strict_schema(MomentPicks))
    # Optional -> anyOf [T, null]
    assert {"type": "null"} in props["reject_reason"]["anyOf"]
    layout_types = props["layout"]["anyOf"]
    assert {"type": "null"} in layout_types
    assert any(t.get("enum") == ["facecam_split", "face_track", "gameplay_fit", "vertical_native"]
               for t in layout_types)
    # verschachtelte Objekte inline und geschlossen
    zoom = props["zooms"]["items"]
    assert zoom["type"] == "object" and zoom["additionalProperties"] is False
    assert zoom["properties"]["focus"]["enum"] == ["face", "gameplay", "center"]
    emph = props["emphasis"]["items"]
    assert emph["required"] == ["t", "emoji", "sfx"]
    sfx = emph["properties"]["sfx"]["anyOf"]
    assert any("record_scratch" in (t.get("enum") or []) for t in sfx)
    assert props["titles"]["required"] == ["youtube", "tiktok", "instagram"]
    # Felder mit Default, aber nicht Optional, bleiben nicht-nullable
    assert props["is_unsafe"] == {"type": "boolean"}
    assert props["content_language"] == {"type": "string"}


def test_moment_picks_details():
    s = strict_schema(MomentPicks)
    item = s["properties"]["moments"]["items"]
    assert item["required"] == ["start", "end", "score", "why"]
    assert item["properties"]["score"] == {"type": "integer"}


def test_strict_schema_returns_independent_copies():
    a = strict_schema(EditPlan)
    a["properties"]["keep"]["type"] = "string"
    assert strict_schema(EditPlan)["properties"]["keep"]["type"] == "boolean"


class _Inner(BaseModel):
    title: str = Field(default="x", max_length=5, description="property named title must survive")


class _Constrained(BaseModel):
    n: int = Field(default=3, ge=0, le=10)
    name: str = Field(pattern=r"^a", min_length=1, max_length=9)
    when: str = Field(json_schema_extra={"format": "date-time"})
    email: str = Field(json_schema_extra={"format": "email"})
    tags: list[str] = Field(default_factory=list, max_length=4)
    mode: Literal["a", "b"] = "a"
    only: Literal["x"] = "x"
    pair: tuple[float, str] | None = None
    inner: _Inner | None = None
    inners: list[_Inner] = Field(default_factory=list)


def test_strict_schema_drops_constraints_and_keeps_useful_keywords():
    s = strict_schema(_Constrained)
    _assert_strict(s)
    props = s["properties"]
    assert props["n"] == {"type": "integer"}
    assert props["name"] == {"type": "string"}
    assert props["when"] == {"type": "string", "format": "date-time"}
    assert props["email"] == {"type": "string"}
    assert props["mode"]["enum"] == ["a", "b"]
    assert props["only"]["const"] == "x"
    pair = props["pair"]["anyOf"][0]
    assert pair["type"] == "array" and {"type": "number"} in pair["items"]["anyOf"]
    inner = props["inner"]["anyOf"][0]
    assert inner["properties"]["title"]["description"] == "property named title must survive"
    assert inner["required"] == ["title"]


class _Node(BaseModel):
    child: _Node | None = None


def test_recursive_schema_is_rejected():
    with pytest.raises(ValueError):
        strict_schema(_Node)


def test_extract_json_object_handles_fences_prose_and_braces_in_strings():
    text = 'Sure! Here it is:\n```json\n{"a": "x}y{", "b": {"c": "\\"q\\""}}\n```\nThanks'
    assert json.loads(_extract_json_object(text)) == {"a": "x}y{", "b": {"c": '"q"'}}
    assert json.loads(_extract_json_object('prefix {"k": [1, {"z": 2}]} suffix {"other": 1}')) == {
        "k": [1, {"z": 2}]}
    with pytest.raises(_ParseError):
        _extract_json_object("no json here")
    with pytest.raises(_ParseError):
        _extract_json_object('{"cut": "off')


def test_parse_json_model_normalizes_tuple_lists():
    plan = {
        "keep": True, "virality_score": 80, "reasoning": "r", "trim_start": 0.0, "trim_end": 10.0,
        "remove_ranges": [[1.0, 2.0, 3.0], [4.2], ["a", 1.0], [5.0, 6.0], "x", [True, 1.0]],
        "hook_text": "h", "titles": {"youtube": "a", "tiktok": "b", "instagram": "c"},
        "descriptions": {"youtube": "a", "tiktok": "b", "instagram": "c"},
    }
    out = _parse_json_model(json.dumps(plan), EditPlan)
    assert out.remove_ranges == [(1.0, 2.0), (5.0, 6.0)]


def test_parse_json_model_reports_validation_errors():
    with pytest.raises(_ParseError) as ei:
        _parse_json_model('{"moments": [{"start": 1}]}', MomentPicks)
    assert "moments.0.end" in str(ei.value)
    picks = _parse_json_model('```\n{"moments": []}\n```', MomentPicks)
    assert picks.moments == []
