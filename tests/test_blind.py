from __future__ import annotations

import base64
import hashlib
import importlib
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

blind = importlib.import_module("edlb.blind")
SubmissionLedger = blind.SubmissionLedger
SubmissionLimitError = blind.SubmissionLimitError
filter_public_result = blind.filter_public_result
manifest_hash = blind.manifest_hash
register_manifest_hash = blind.register_manifest_hash
scan_canaries = blind.scan_canaries
sign_result = blind.sign_result
verify_result = blind.verify_result


class BlindTest(unittest.TestCase):
    def test_submission_limit_and_exact_manifest_hash(self) -> None:
        manifest = b'{"seed": 7}\n'
        with (
            tempfile.TemporaryDirectory() as directory,
            SubmissionLedger(
                Path(directory) / "ledger.sqlite", trusted_timestamps=True
            ) as ledger,
        ):
            digest = register_manifest_hash(
                ledger, "team-a", "run-1", manifest, "2026-01-01T00:00:00Z"
            )
            self.assertEqual(digest, "sha256:" + hashlib.sha256(manifest).hexdigest())
            ledger.record_submission("team-a", "2026-01-15T00:00:00+00:00", b"second")
            with self.assertRaises(SubmissionLimitError):
                ledger.record_submission("team-a", "2026-01-20T00:00:00Z", b"third")
            self.assertEqual(ledger.recent_count("team-a", "2026-01-20T00:00:00Z"), 2)
            with self.assertRaises(ValueError):
                ledger.record_submission(
                    "team-b", "2026-01-01T00:00:00-08:00", b"offset"
                )

    def test_submission_ledger_accepts_timestamp_only(self) -> None:
        with SubmissionLedger(trusted_timestamps=True) as ledger:
            ledger.record_submission("team-a", "2026-01-01T00:00:00Z")
            ledger.record_submission("team-a", "2026-01-02T00:00:00Z")
            with self.assertRaises(SubmissionLimitError):
                ledger.record_submission("team-a", "2026-01-03T00:00:00Z")

    def test_untrusted_timestamps_cannot_bypass_quota(self) -> None:
        times = iter(
            [
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 2, tzinfo=UTC),
                datetime(2026, 1, 3, tzinfo=UTC),
            ]
        )
        with SubmissionLedger(clock=lambda: next(times)) as ledger:
            ledger.record_submission("team-a", "1900-01-01T00:00:00Z")
            ledger.record_submission("team-a", "2999-01-01T00:00:00Z")
            with self.assertRaises(SubmissionLimitError):
                ledger.record_submission("team-a", "4000-01-01T00:00:00Z")
            self.assertEqual(
                ledger.recent_count("team-a", "2026-01-03T00:00:00Z"), 2
            )

    def test_team_ids_must_be_canonical(self) -> None:
        with SubmissionLedger(clock=lambda: datetime(2026, 1, 1, tzinfo=UTC)) as ledger:
            with self.assertRaises(ValueError):
                ledger.record_submission(" team-a", "2026-01-01T00:00:00Z")
            with self.assertRaises(ValueError):
                ledger.record_submission("a" * 129, "2026-01-01T00:00:00Z")

    def test_canaries_scan_bytes_and_common_encodings(self) -> None:
        token = b"canary-secret"
        self.assertEqual(
            scan_canaries(b"safe canary-secret", b"trace", (token,)),
            ("canary-secret",),
        )
        self.assertEqual(
            scan_canaries(b"safe", base64.b64encode(token), (token,)),
            ("canary-secret",),
        )
        self.assertEqual(
            scan_canaries(token.hex().encode(), b"trace", (token,)),
            ("canary-secret",),
        )
        self.assertEqual(scan_canaries(b"safe", b"trace", (token,)), ())

    def test_public_result_and_signature(self) -> None:
        result = {
            "category_scores": {
                "evidence_and_understanding": 0.9,
                "oracle-secret": 0.2,
            },
            "reliability": {"pass_at_1": 0.8, "oracle-secret": 0.1},
            "cost": {"minor_units": 12, "currency": "private"},
            "errors": {
                "count": 2,
                "categories": {"timeout": 1, "oracle-secret": 1},
                "message": "private trace",
            },
            "trace": [{"secret": True}],
            "assertion_targets": ["oracle-1"],
        }
        public = filter_public_result(result)
        self.assertEqual(
            public,
            {
                "category_scores": {"evidence_and_understanding": 0.9},
                "reliability": {"pass_at_1": 0.8},
                "cost": 12,
                "error_summary": {"count": 2, "categories": {"timeout": 1}},
            },
        )
        signature = sign_result(public, "secret")
        self.assertTrue(verify_result(public, signature, "secret"))
        self.assertFalse(
            verify_result({**public, "cost": {"minor_units": 13}}, signature, "secret")
        )
        scorecard = filter_public_result(
            {
                "category_scores": {"crm_integrity": 1.0},
                "reliability": {"pass_at_1": 1.0},
                "resource_usage": {"cost_minor_units": 9, "errors": 2},
                "assertions": [{"assertion_id": "secret"}],
            }
        )
        self.assertEqual(scorecard["cost"], 9)
        self.assertEqual(scorecard["error_summary"], {"count": 2})
        self.assertNotIn("assertions", scorecard)

    def test_public_result_rejects_raw_private_values(self) -> None:
        public = filter_public_result(
            {
                "category_scores": {"private": "oracle-secret"},
                "reliability": {"input_validation": {"errors": ["secret"]}},
                "cost": "canary-secret",
                "error_summary": {
                    "count": 3,
                    "categories": {"private": 1, "invalid_action": 2},
                    "raw": "oracle-secret",
                },
            }
        )
        encoded = json.dumps(public)
        self.assertEqual(public["category_scores"], {})
        self.assertEqual(public["reliability"], {})
        self.assertNotIn("canary-secret", encoded)
        self.assertNotIn("oracle-secret", encoded)
        self.assertNotIn("private", encoded)
        self.assertEqual(
            public["error_summary"],
            {"count": 3, "categories": {"invalid_action": 2}},
        )

    def test_public_result_rejects_out_of_range_numbers(self) -> None:
        public = filter_public_result(
            {
                "category_scores": {
                    "evidence_and_understanding": 1.1,
                    "crm_integrity": -0.1,
                },
                "reliability": {
                    "pass_at_1": 1.1,
                    "pass_at_3": -0.1,
                    "worlds": -1,
                    "confidence_interval": [0.1, 1.1],
                },
                "cost": -1,
            }
        )
        self.assertEqual(public["category_scores"], {})
        self.assertEqual(public["reliability"], {})
        self.assertNotIn("cost", public)

    def test_manifest_hash_uses_exact_bytes(self) -> None:
        value = '{"seed": 7}\n'
        self.assertEqual(
            manifest_hash(value), "sha256:" + hashlib.sha256(value.encode()).hexdigest()
        )
        self.assertNotEqual(manifest_hash(json.loads(value)), manifest_hash(value))


if __name__ == "__main__":
    unittest.main()
