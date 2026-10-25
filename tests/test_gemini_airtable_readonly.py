from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from windows_execution_foundation import gemini_airtable_readonly as subject


CONFIG = {"secure_store_module": "secure.py", "credential_path": "pat.bin", "base_id": "evil", "table_id": "evil"}


class FakeTypes:
    class FunctionDeclaration:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    class Tool:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    class GenerateContentConfig:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    class AutomaticFunctionCallingConfig:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)
    class Part:
        @staticmethod
        def from_function_response(*, name, response): return SimpleNamespace(name=name, response=response)
    class Content:
        def __init__(self, **kwargs): self.__dict__.update(kwargs)


def response(*, call=None, text="final"):
    part = SimpleNamespace(function_call=call) if call else SimpleNamespace(function_call=None, text=text)
    content = SimpleNamespace(parts=[part])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)], text=text)


class ReadonlyTests(unittest.TestCase):
    def test_fixed_base_and_table_and_get_only(self):
        request = mock.Mock(return_value={"records": []})
        subject.search_management_os(CONFIG, source="projects", request_fn=request)
        cfg, method = request.call_args.args
        self.assertEqual(cfg["base_id"], subject.BASE_ID)
        self.assertEqual(cfg["table_id"], subject.TABLES["projects"])
        self.assertEqual(method, "GET")
        self.assertNotIn("body", request.call_args.kwargs)

    def test_rejects_non_allowlisted_table(self):
        with self.assertRaisesRegex(subject.ReadonlyContractError, "SOURCE_NOT_ALLOWED"):
            subject.search_management_os(CONFIG, source="secrets")

    def test_field_allowlist_blocks_raw_fields(self):
        request = mock.Mock(return_value={"records": [{"id": "rec12345678901", "fields": {
            "Action ID": "a1", "Password": "leak", "Unknown": "raw"
        }}]})
        result = subject.search_management_os(CONFIG, source="next_actions", request_fn=request)
        fields = result["records"][0]["fields"]
        self.assertEqual(fields, {"action_id": "a1"})

    def test_limit_is_clamped(self):
        records = [{"id": f"rec{i:011d}", "fields": {"Action ID": str(i)}} for i in range(30)]
        result = subject.search_management_os(CONFIG, source="next_actions", limit=999,
                                              request_fn=mock.Mock(return_value={"records": records}))
        self.assertEqual(len(result["records"]), subject.MAX_LIMIT)

    def test_query_normalization(self):
        self.assertEqual(subject.normalize_query("  alpha\x00\n beta  "), "alpha beta")
        self.assertEqual(len(subject.normalize_query("x" * 999)), 160)

    def test_secret_and_pii_redaction(self):
        raw = "api_key=abcdefghi me@example.com +81 90-1234-5678 AIza" + "x" * 25
        safe = subject._redact_text(raw)
        self.assertNotIn("abcdefghi", safe)
        self.assertNotIn("me@example.com", safe)
        self.assertNotIn("90-1234-5678", safe)
        self.assertNotIn("AIza", safe)

    def test_output_is_bounded(self):
        records = [{"id": f"rec{i:011d}", "fields": {"Action": "x" * 5000}} for i in range(30)]
        result = subject.search_management_os(CONFIG, source="next_actions", limit=20,
                                              request_fn=mock.Mock(return_value={"records": records}))
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))), subject.MAX_TOOL_OUTPUT_CHARS)

    def test_get_validates_record_and_uses_get(self):
        request = mock.Mock(return_value={"id": "rec12345678901", "fields": {"Name": "asset"}})
        result = subject.get_management_os_record(CONFIG, source="assets", record_id="rec12345678901", request_fn=request)
        self.assertEqual(request.call_args.args[1], "GET")
        self.assertEqual(result["record"]["fields"]["name"], "asset")
        with self.assertRaisesRegex(subject.ReadonlyContractError, "RECORD_ID_INVALID"):
            subject.get_management_os_record(CONFIG, source="assets", record_id="../../bad", request_fn=request)

    def test_tool_contract_has_only_two_tools_and_enum_sources(self):
        tool = subject._tool_specs(FakeTypes)[0]
        declarations = tool.function_declarations
        self.assertEqual([item.name for item in declarations], ["search_management_os", "get_management_os_record"])
        for item in declarations:
            self.assertEqual(item.parameters_json_schema["properties"]["source"]["enum"], list(subject.TABLES))

    def test_manual_loop_returns_final_answer_after_tool_result(self):
        call = SimpleNamespace(name="search_management_os", args={"source": "assets", "query": "bridge"})
        generate = mock.Mock(side_effect=[response(call=call, text=""), response(text="grounded final")])
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        request = mock.Mock(return_value={"records": [{"id": "rec12345678901", "fields": {"Name": "bridge"}}]})
        result = subject.run_manual_tool_loop(client, CONFIG, "question", types_module=FakeTypes, request_fn=request)
        self.assertEqual(result["answer"], "grounded final")
        self.assertEqual(result["gemini_api_call_count"], 2)
        self.assertEqual(result["function_call_count"], 1)
        self.assertEqual(result["airtable_get_count"], 1)
        second_contents = generate.call_args_list[1].kwargs["contents"]
        self.assertEqual(second_contents[-1].parts[0].name, "search_management_os")

    def test_tool_call_limit(self):
        call = SimpleNamespace(name="search_management_os", args={"source": "assets"})
        generate = mock.Mock(side_effect=[response(call=call, text="")] * (subject.MAX_TOOL_CALLS + 1))
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        with self.assertRaisesRegex(RuntimeError, "TOOL_CALL_LIMIT"):
            subject.run_manual_tool_loop(client, CONFIG, "q", types_module=FakeTypes,
                                         request_fn=mock.Mock(return_value={"records": []}))
        self.assertEqual(generate.call_count, subject.MAX_TOOL_CALLS + 1)

    def test_source_code_contains_no_airtable_write_method(self):
        source = Path(subject.__file__).read_text(encoding="utf-8")
        for method in ('"POST"', '"PATCH"', '"PUT"', '"DELETE"'):
            self.assertNotIn(method, source)


if __name__ == "__main__":
    unittest.main()
