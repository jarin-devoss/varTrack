"""
tests/unit/test_strategy_selector.py

Unit tests for _decide_sync_mode() strategy selection in stage_sync.py.

The real implementation is a size-based heuristic, not a latency-based one:
  > 500 keys  →  GIT_SMART_REPAIR
  ≤ 500 keys  →  GIT_UPSERT_ALL
  explicit forced_mode (not UNSPECIFIED/AUTO) → always honoured
"""
from __future__ import annotations

import unittest

from app.pipeline.stage_sync import _decide_sync_mode
from app.utils.enums.sync_mode import SyncMode


class TestDecideSyncMode(unittest.TestCase):

    def test_small_payload_uses_upsert_all(self):
        mode = _decide_sync_mode(key_count=10, forced_mode=SyncMode.UNSPECIFIED)
        self.assertEqual(mode, SyncMode.GIT_UPSERT_ALL)

    def test_exactly_500_keys_uses_upsert_all(self):
        mode = _decide_sync_mode(key_count=500, forced_mode=SyncMode.UNSPECIFIED)
        self.assertEqual(mode, SyncMode.GIT_UPSERT_ALL)

    def test_over_500_keys_uses_smart_repair(self):
        mode = _decide_sync_mode(key_count=501, forced_mode=SyncMode.UNSPECIFIED)
        self.assertEqual(mode, SyncMode.GIT_SMART_REPAIR)

    def test_large_payload_uses_smart_repair(self):
        mode = _decide_sync_mode(key_count=10_000, forced_mode=SyncMode.UNSPECIFIED)
        self.assertEqual(mode, SyncMode.GIT_SMART_REPAIR)

    def test_forced_upsert_all_honoured(self):
        # Forced mode overrides heuristic even for large payload
        mode = _decide_sync_mode(key_count=10_000, forced_mode=SyncMode.GIT_UPSERT_ALL)
        self.assertEqual(mode, SyncMode.GIT_UPSERT_ALL)

    def test_forced_smart_repair_honoured(self):
        # Forced mode overrides heuristic even for small payload
        mode = _decide_sync_mode(key_count=5, forced_mode=SyncMode.GIT_SMART_REPAIR)
        self.assertEqual(mode, SyncMode.GIT_SMART_REPAIR)

    def test_auto_mode_falls_back_to_heuristic(self):
        # AUTO is treated like UNSPECIFIED
        mode_small = _decide_sync_mode(key_count=10, forced_mode=SyncMode.AUTO)
        mode_large = _decide_sync_mode(key_count=1000, forced_mode=SyncMode.AUTO)
        self.assertEqual(mode_small, SyncMode.GIT_UPSERT_ALL)
        self.assertEqual(mode_large, SyncMode.GIT_SMART_REPAIR)

    def test_zero_keys_uses_upsert_all(self):
        mode = _decide_sync_mode(key_count=0, forced_mode=SyncMode.UNSPECIFIED)
        self.assertEqual(mode, SyncMode.GIT_UPSERT_ALL)


if __name__ == "__main__":
    unittest.main()
