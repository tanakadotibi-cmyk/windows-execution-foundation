from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from windows_execution_foundation import gemini_design_review as subject


class FakeTypes:
    class GenerateContentConfig:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)

    class ThinkingConfig:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)

    class ThinkingLevel:
        MINIMAL = "MINIMAL"


def packet(**updates):
    value = {field: ([] if field in subject.ARRAY_FIELDS else "") for field in subject.PACKET_FIELDS}
    value.update(trigger_phrase=subject.TRIGGER_PHRASE, objective="Review the existing design")
    value.update(updates)
    return value


def review_json(**updates):
    value = {
        "objective_assessment": "bounded",
        "reusable_assets": ["existing wrapper"],
        "missing_parts": [],
        "minimum_design": "reuse the wrapper",
        "strongest_counterargument": "the wrapper may be too narrow",
        "simpler_alternative": "keep the deterministic path",
        "deterministic_scope": ["validation"],
        "ai_scope": ["design judgment"],
        "execution_route": "existing route",
        "acceptance_conditions": ["focused test passes"],
        "rollback_plan": "remove the isolated review",
        "unknowns": [],
        "next_moves": ["single live canary later"],
    }
    value.update(updates)
    return json.dumps(value, ensure_ascii=False)


class FakeClient:
    def __init__(self, generate):
        self.models = SimpleNamespace(generate_content=generate)

    def __enter__(self): return self
    def __exit__(self, *_args): return False


class DesignReviewTests(unittest.TestCase):
    def run_review(self, raw, *, generate=None):
        generate = generate or mock.Mock(return_value=SimpleNamespace(text=review_json()))
        factory = mock.Mock(return_value=FakeClient(generate))
        result = subject.review_packet(raw, key="fake-offline-key", client_factory=factory,
                                       types_module=FakeTypes)
        return result, factory, generate

    def test_exact_trigger_calls_once_and_mismatch_calls_zero(self):
        result, factory, generate = self.run_review(packet())
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(result["gemini_api_call_count"], 1)
        mismatch, mismatch_factory, mismatch_generate = self.run_review(
            packet(trigger_phrase="AIスタジオレビュー")
        )
        self.assertEqual(mismatch["status"], "TRIGGER_MISMATCH")
        self.assertEqual(mismatch["gemini_api_call_count"], 0)
        mismatch_factory.assert_not_called()
        mismatch_generate.assert_not_called()
        factory.assert_called_once()

    def test_invalid_and_oversize_packets_fail_closed_without_call(self):
        for raw in ({"unknown": "field"}, packet(objective="x" * (subject.MAX_STRING_CHARS + 1))):
            with self.subTest(raw_type=type(raw).__name__):
                result, factory, generate = self.run_review(raw)
                self.assertEqual(result["status"], "INVALID_PACKET")
                self.assertFalse(result["review_executed"])
                self.assertEqual(result["gemini_api_call_count"], 0)
                factory.assert_not_called()
                generate.assert_not_called()

    def test_secret_email_phone_and_local_path_are_redacted(self):
        raw = packet(objective=(
            "api_key=supersecretvalue me@example.com +81 90-1234-5678 "
            r"C:\Users\epuro\private\plan.txt"
        ))
        result, _factory, generate = self.run_review(raw)
        self.assertEqual(result["status"], "PASS")
        prompt = generate.call_args.kwargs["contents"]
        for forbidden in ("supersecretvalue", "me@example.com", "90-1234-5678", r"C:\Users\epuro"):
            self.assertNotIn(forbidden, prompt)
        for marker in ("[REDACTED_SECRET]", "[REDACTED_EMAIL]", "[REDACTED_PHONE]", "[REDACTED_LOCAL_PATH]"):
            self.assertIn(marker, prompt)

    def test_api_exception_is_failed_without_retry(self):
        generate = mock.Mock(side_effect=RuntimeError("offline fake failure"))
        result, _factory, generate = self.run_review(packet(), generate=generate)
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse(result["review_executed"])
        self.assertEqual(result["retry_count"], 0)
        self.assertEqual(generate.call_count, 1)

    def test_structured_pass_preserves_required_design_scopes(self):
        expected = {
            "minimum_design": "minimum retained",
            "strongest_counterargument": "counterargument retained",
            "deterministic_scope": ["deterministic retained"],
            "ai_scope": ["AI retained"],
        }
        generate = mock.Mock(return_value=SimpleNamespace(text=review_json(**expected)))
        result, _factory, _generate = self.run_review(packet(), generate=generate)
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["review_executed"])
        for key, value in expected.items():
            self.assertEqual(result[key], value)

    def test_instruction_prioritizes_existing_assets_and_deterministic_first(self):
        instruction = subject.SYSTEM_INSTRUCTION
        self.assertIn("reuse an existing asset", instruction)
        self.assertIn("Deterministic-First", instruction)

    def test_results_report_zero_external_and_airtable_mutations(self):
        for raw in (packet(), packet(trigger_phrase="not exact")):
            with self.subTest(trigger=raw["trigger_phrase"]):
                result, _factory, _generate = self.run_review(raw)
                self.assertEqual(result["external_mutation_count"], 0)
                self.assertEqual(result["airtable_write_count"], 0)


if __name__ == "__main__":
    unittest.main()
