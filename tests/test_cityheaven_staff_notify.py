from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from windows_execution_foundation.cityheaven_staff_notify import ENDPOINT, EVENT, notify


KEY = "ch_" + "a" * 64
OCCURRED_AT = "2026-09-28T03:04:05Z"


class CityHeavenStaffNotifyTests(unittest.TestCase):
    def test_endpoint_is_exact_live_route(self):
        self.assertEqual(
            ENDPOINT,
            "https://linehook.tantan-ops.com/line-webhook/cityheaven-notify",
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.secure_module = self.root / "secure_store.py"
        self.secure_module.write_text(
            "def read_secret(path):\n"
            "    return b'synthetic-test-token' if path.exists() else None\n",
            encoding="utf-8",
        )
        self.credential = self.root / "internal-token.dpapi"
        self.credential.write_bytes(b"synthetic-encrypted-placeholder")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"cityheaven_staff_notify": {
            "secure_store_module": str(self.secure_module),
            "credential_path": str(self.credential),
        }}), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_exact_three_field_payload_hash_only_and_stable_replay(self):
        calls = []

        def transport(request, timeout):
            calls.append((request, timeout))
            return 200, b'{"status":"notified"}'

        state = self.root / "existing-job" / "send-state.json"
        first = notify(self.config, KEY, OCCURRED_AT, state, transport=transport)
        replay = notify(self.config, KEY, OCCURRED_AT, state, transport=transport)
        self.assertEqual(first["status"], "notified")
        self.assertEqual(replay["status"], "already_notified")
        self.assertEqual(len(calls), 1)
        request, timeout = calls[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.method, "POST")
        self.assertEqual(timeout, 20)
        self.assertEqual(request.get_header("User-agent"), "Tantan-Foundation/1.0")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(set(payload), {"event", "reservation_key", "occurred_at"})
        self.assertEqual(payload, {
            "event": EVENT, "reservation_key": KEY, "occurred_at": OCCURRED_AT,
        })
        serialized = request.data.decode("utf-8").lower()
        for forbidden in (
            "raw-guard", "customer", "phone", "email", "my_heaven_id", "rsvid",
            "chat", "course", "message", "body",
        ):
            self.assertNotIn(forbidden, serialized)
        ledger = state.read_text(encoding="utf-8").lower()
        self.assertNotIn("synthetic-test-token", ledger)
        self.assertNotIn("raw-guard", ledger)
        self.assertEqual(first["duplicate_count"], 0)
        self.assertEqual(replay["duplicate_count"], 0)

    def test_200_already_notified_is_success(self):
        result = notify(
            self.config, KEY, OCCURRED_AT, self.root / "already.json",
            transport=lambda *_: (200, b'{"status":"already_notified"}'),
        )
        self.assertEqual(result["status"], "already_notified")
        self.assertEqual(result["reason_code"], "STAFF_ALREADY_NOTIFIED")
        self.assertEqual(result["http_status"], 200)

    def test_401_and_403_fail_closed_without_retry(self):
        for status in (401, 403):
            with self.subTest(status=status):
                calls = []
                result = notify(
                    self.config, KEY, OCCURRED_AT, self.root / f"auth-{status}.json",
                    transport=lambda *args: calls.append(args) or (status, b'{}'),
                )
                self.assertEqual(result["reason_code"], "STAFF_NOTIFY_AUTH_FAILED")
                self.assertEqual(result["network_call_count"], 1)
                self.assertEqual(len(calls), 1)

    def test_429_5xx_and_network_fail_once_and_remain_uncertain(self):
        cases = (
            ("rate", lambda *_: (429, b'{}'), "STAFF_NOTIFY_RATE_LIMITED"),
            ("upstream", lambda *_: (503, b'{}'), "STAFF_NOTIFY_UPSTREAM_FAILED"),
            ("network", lambda *_: (_ for _ in ()).throw(OSError("offline")),
             "STAFF_NOTIFY_NETWORK_FAILED"),
        )
        for name, transport, reason in cases:
            with self.subTest(name=name):
                state = self.root / f"{name}.json"
                first = notify(self.config, KEY, OCCURRED_AT, state, transport=transport)
                calls = []
                replay = notify(
                    self.config, KEY, OCCURRED_AT, state,
                    transport=lambda *args: calls.append(args) or (200, b'{"status":"notified"}'),
                )
                self.assertEqual(first["reason_code"], reason)
                self.assertEqual(first["network_call_count"], 1)
                self.assertEqual(replay["reason_code"], "STAFF_NOTIFY_UNCERTAIN")
                self.assertEqual(calls, [])

    def test_not_provisioned_makes_no_network_call_and_no_state(self):
        missing_config = self.root / "missing-config.json"
        missing_config.write_text("{}", encoding="utf-8")
        calls = []
        state = self.root / "not-provisioned.json"
        result = notify(
            missing_config, KEY, OCCURRED_AT, state,
            transport=lambda *args: calls.append(args) or (200, b'{"status":"notified"}'),
        )
        self.assertEqual(result["reason_code"], "AUTH_REQUIRED_INTERNAL_TOKEN_NOT_PROVISIONED")
        self.assertEqual(result["network_call_count"], 0)
        self.assertEqual(calls, [])
        self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
