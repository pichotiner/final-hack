from __future__ import annotations

import json
import unittest
from pathlib import Path

from adapters import IdMap, MappingError, from_1c_order, from_galaxy_order, from_kompas_assembly, from_mes_operation, to_erp_quality_result

EXAMPLES = Path(__file__).parent / "examples"


def example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


class AdapterTest(unittest.TestCase):
    def test_vendor_orders_share_internal_shape_and_conflict_is_visible(self):
        ids = IdMap()
        first = from_1c_order(example("1c-order.json"), ids)
        second = from_galaxy_order(example("galaxy-order.json"), ids)
        self.assertEqual(first["item_ids"], second["item_ids"])
        self.assertEqual(ids.resolve("1c", "work_order", "001"), "order-001")
        with self.assertRaises(MappingError):
            ids.bind("1c", "work_order", "001", "different")

    def test_mes_requires_known_identity_and_kompas_has_no_geometry(self):
        ids = IdMap()
        with self.assertRaises(MappingError):
            from_mes_operation(example("mes-finished.json"), ids)
        ids.bind("mes", "item", "MES-assembly-001", "assembly-001")
        event = from_mes_operation(example("mes-finished.json"), ids)
        self.assertEqual(event["item_id"], "assembly-001")
        self.assertEqual(event["event_type"], "operation_finished")
        assembly = from_kompas_assembly(example("kompas-assembly.json"))
        self.assertFalse(assembly["geometry_available"])
        self.assertEqual(len(assembly["components"]), 2)

    def test_result_preserves_version_and_previous_message(self):
        result = to_erp_quality_result("result-v2", "order-001", "assembly-001", 2, "rework", ["card-1"], "unresolved", True, "result-v1")
        self.assertEqual(result["previous_message_id"], "result-v1")
        self.assertEqual(result["result_version"], 2)


if __name__ == "__main__":
    unittest.main()
