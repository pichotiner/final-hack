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
