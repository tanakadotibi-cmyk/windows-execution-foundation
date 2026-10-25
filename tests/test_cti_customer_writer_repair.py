from __future__ import annotations

import ast
import collections
import math
import sys
import types
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / "local" / "cti-customer-deterministic-projection-v01-20260930" / "candidate"


def function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name)
    return ast.get_source_segment(source, node) or ""


class CtiCustomerWriterRepairTests(unittest.TestCase):
    def test_customer_tables_are_registered_with_exact_writable_field_sets(self) -> None:
        watcher = (CANDIDATE / "bridge_watcher.py").read_text(encoding="utf-8")
        registry = function_source(CANDIDATE / "bridge_watcher.py", "_airtable_writable_fields")
        self.assertIn("CTI_CUSTOMER_ENTITY_TABLE: set(CTI_CUSTOMER_ENTITY_FIELDS.values())", registry)
        self.assertIn("CTI_CUSTOMER_JOURNEY_TABLE: set(CTI_CUSTOMER_JOURNEY_FIELDS.values())", registry)
        self.assertEqual(watcher.count("CTI_CUSTOMER_ENTITY_TABLE: set(CTI_CUSTOMER_ENTITY_FIELDS.values())"), 1)
        self.assertEqual(watcher.count("CTI_CUSTOMER_JOURNEY_TABLE: set(CTI_CUSTOMER_JOURNEY_FIELDS.values())"), 1)

        names = (
            "ACTIONS EVENTS BUSINESS_IMPACTS ASSETS RECONCILIATION_RUNS ATTENTION_QUEUE "
            "OS_SNAPSHOTS TIME_OBSERVATIONS DAILY_TIME_SUMMARIES WORK_SESSIONS "
            "RECRUITING_FUNNEL_EVENTS RECRUITING_APPLICANTS PROGRAM_TABLE BUSINESS_METRICS"
        ).split()
        namespace: dict[str, Any] = {name: f"table-{name}" for name in names}
        for name in ("F", "ACTION_WRITABLE_FIELD_TYPES", "EF", "BIF", "AF", "V2AF", "RF", "ATF",
                     "SF", "V2SF", "V2NF", "TOF", "DTSF", "WSF", "RJF", "RAF", "NBPF"):
            namespace[name] = collections.defaultdict(lambda: f"field-{name}")
        namespace.update({
            "CTI_CUSTOMER_ENTITY_TABLE": "entity-table",
            "CTI_CUSTOMER_JOURNEY_TABLE": "journey-table",
            "CTI_CUSTOMER_ENTITY_FIELDS": {"dedup": "entity-dedup", "customer_id": "customer-id"},
            "CTI_CUSTOMER_JOURNEY_FIELDS": {"dedup": "journey-dedup", "customer": "customer-link"},
            "AirtableWriteContractError": RuntimeError,
            "PRIORITY_SCORE_FIELD": "priority",
            "MONITOR_STATE_SELECT_OPTIONS": frozenset(),
            "math": math,
        })
        guard = function_source(CANDIDATE / "bridge_watcher.py", "guard_airtable_write_request")
        exec(registry + "\n\n" + guard, namespace)
        for table, fields in (
            ("entity-table", namespace["CTI_CUSTOMER_ENTITY_FIELDS"]),
            ("journey-table", namespace["CTI_CUSTOMER_JOURNEY_FIELDS"]),
        ):
            body = {"records": [{"fields": {field: "value" for field in fields.values()}}]}
            self.assertIs(namespace["guard_airtable_write_request"]("POST", table, body), body)

    def test_customer_failure_detail_is_retained_in_writer_receipt(self) -> None:
        pending = function_source(CANDIDATE / "bridge_watcher.py", "apply_pending_cti_daily")
        collector = function_source(CANDIDATE / "cti_daily_collector.py", "apply_canonical_writer_once")
        self.assertIn('"first_failed_boundary": "CTI_CUSTOMER_PROJECTION_APPLY"', pending)
        self.assertIn('"customer_results"', collector)
        self.assertIn('"error_type"', collector)
        self.assertIn('"error"', collector)
        self.assertIn('"first_failed_boundary"', collector)

        captured: dict[str, Any] = {}
        bridge = types.ModuleType("bridge_watcher")
        bridge.apply_pending_cti_daily = lambda: {
            "status": "PARTIAL",
            "customer_results": [{
                "business_date": "20260929",
                "status": "FAILED",
                "first_failed_boundary": "CTI_CUSTOMER_PROJECTION_APPLY",
                "error_type": "AirtableWriteContractError",
                "error": "AIRTABLE_WRITE_TABLE_UNREGISTERED",
            }],
        }
        previous = sys.modules.get("bridge_watcher")
        sys.modules["bridge_watcher"] = bridge
        namespace = {
            "Any": Any,
            "Path": Path,
            "WRITER_LAST_RUN_PATH": Path("writer-last-run.json"),
            "_writer_count": lambda result, field: int(result.get(field, 0) or 0),
            "_safe_writer_error": lambda exc: str(exc),
            "atomic_json": lambda path, value: captured.update({"path": path, "value": value}),
            "now": lambda: "2026-09-30T00:00:00Z",
        }
        try:
            exec(collector, namespace)
            receipt = namespace["apply_canonical_writer_once"]("20260929", Path("receipt.json"))
        finally:
            if previous is None:
                sys.modules.pop("bridge_watcher", None)
            else:
                sys.modules["bridge_watcher"] = previous
        self.assertEqual(receipt["first_failed_boundary"], "CTI_CUSTOMER_PROJECTION_APPLY")
        self.assertEqual(receipt["customer_results"][0]["error_type"], "AirtableWriteContractError")
        self.assertEqual(captured["value"], receipt)


if __name__ == "__main__":
    unittest.main()
