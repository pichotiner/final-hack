import copy

import pytest

from contracts.compatibility import differences, shape
from contracts.validation import document


@pytest.mark.parametrize(
    "change",
    [
        lambda x: x["components"]["schemas"]["InspectionResult"]["enum"].pop(),
        lambda x: x["components"]["schemas"]["InspectionPayload"]["required"].append(
            "confidence"
        ),
        lambda x: x["components"]["schemas"]["NullableId"]["anyOf"].pop(),
        lambda x: x["components"]["schemas"]["InspectionPayload"]["properties"][
            "defects"
        ]["items"].update(type="string"),
        lambda x: x["paths"]["/api/v2/items"]["get"]["responses"]["200"]["content"][
            "application/json"
        ].update(schema={"type": "string"}),
    ],
)
def test_detects_nested_enum_required_nullability_items_and_response_changes(change):
    original = document()
    changed = copy.deepcopy(original)
    change(changed)
    assert differences(shape(original), shape(changed))


def test_v1_to_v2_route_change_is_explicit_not_compatible():
    new = document()
    old = copy.deepcopy(new)
    old["paths"]["/api/v1/events"] = old["paths"].pop("/api/v2/events")
    hits = differences(shape(old), shape(new))
    assert "/paths//api/v1/events" in hits and "/paths//api/v2/events" in hits


@pytest.mark.parametrize(
    "name",
    ["description", "title", "summary", "example", "examples", "servers", "tags"],
)
def test_documentation_named_properties_still_have_checked_types(name):
    original = {
        "paths": {},
        "components": {
            "schemas": {
                "Payload": {
                    "type": "object",
                    "properties": {name: {"type": "string"}},
                }
            }
        },
    }
    changed = copy.deepcopy(original)
    changed["components"]["schemas"]["Payload"]["properties"][name]["type"] = "integer"
    assert differences(shape(original), shape(changed)) == [
        f"/components/schemas/Payload/properties/{name}/type"
    ]


def test_named_schemas_and_nested_properties_preserve_names_but_ignore_annotations():
    original = {
        "paths": {},
        "components": {
            "schemas": {
                "description": {
                    "type": "object",
                    "properties": {
                        "properties": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "old"}
                            },
                        }
                    },
                }
            }
        },
    }
    changed = copy.deepcopy(original)
    field = changed["components"]["schemas"]["description"]["properties"]["properties"][
        "properties"
    ]["title"]
    field["description"] = "new documentation"
    assert shape(original) == shape(changed)
    field["type"] = "integer"
    assert differences(shape(original), shape(changed))


@pytest.mark.parametrize("keyword", ["const", "enum", "default"])
def test_literal_objects_are_not_treated_as_documentation(keyword):
    value = {"description": "old"}
    original = {
        "paths": {},
        "components": {
            "schemas": {
                "Payload": {
                    keyword: [value] if keyword == "enum" else value,
                }
            }
        },
    }
    changed = copy.deepcopy(original)
    literal = changed["components"]["schemas"]["Payload"][keyword]
    (literal[0] if keyword == "enum" else literal)["description"] = "new"
    assert differences(shape(original), shape(changed))


def test_real_defect_description_type_change_is_detected():
    original = document()
    changed = copy.deepcopy(original)
    changed["components"]["schemas"]["DefectSign"]["properties"]["description"] = {
        "type": "integer"
    }
    assert differences(shape(original), shape(changed))
