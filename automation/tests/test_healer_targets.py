"""healer 멀티 타깃 테스트 — 라우팅과 **대상별 서킷 브레이커**를 고정한다.

핵심 회귀 검사: 계열사 A 의 서킷이 열려도 B 의 자동 복구는 계속되어야 한다.
(단일 서버 시절에는 서킷이 전역 하나여서, VM 을 붙이면 A 의 플래핑이 B 를 막았다)

실행: python3 -m unittest discover -s automation/tests -t automation
"""

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# healer 는 import 시점에 EventLog 와 대상 레지스트리를 만든다 → 그 전에 환경을 준다
os.environ.setdefault("EVENT_LOG_DIR", tempfile.mkdtemp())
os.environ.setdefault("TARGET_CONTAINER", "pitwall_web")
os.environ.setdefault("HEALER_TOKEN", "test-token")
os.environ.setdefault("HEAL_TARGETS", json.dumps([
    {"company": "internal", "container": "pitwall_web", "proxy": "socket-proxy:2375"},
    {"company": "A", "container": "pitwall_web", "proxy": "172.31.9.142:2375"},
    {"company": "B", "container": "pitwall_web", "proxy": "172.31.9.33:2375"},
]))

from healing import healer  # noqa: E402


class TargetRegistryTest(unittest.TestCase):
    def test_parses_three_targets(self):
        self.assertEqual(sorted(healer.TARGETS), ["A/pitwall_web", "B/pitwall_web", "internal/pitwall_web"])
        self.assertEqual(healer.TARGETS["A/pitwall_web"]["proxy_host"], "172.31.9.142")
        self.assertEqual(healer.TARGETS["A/pitwall_web"]["proxy_port"], 2375)

    def test_empty_env_falls_back_to_single_local_target(self):
        """HEAL_TARGETS 가 없으면 VM 분리 전 구성 그대로 동작해야 한다."""
        with mock.patch.dict(os.environ, {"HEAL_TARGETS": ""}):
            targets = healer.load_targets()
        self.assertEqual(list(targets), ["internal/pitwall_web"])
        self.assertEqual(targets["internal/pitwall_web"]["proxy_host"], "socket-proxy")

    def test_rejects_malformed_entries(self):
        for bad in ([{"company": "A"}], [], [{"container": "x"}, {"container": "x"}]):
            with mock.patch.dict(os.environ, {"HEAL_TARGETS": json.dumps(bad)}):
                with self.assertRaises(ValueError):
                    healer.load_targets()

    def test_resolve(self):
        self.assertEqual(healer.resolve_target({"company": "A", "container": "pitwall_web"})["company"], "A")
        # company 생략 → internal (감시 서버 자신)
        self.assertEqual(healer.resolve_target({"container": "pitwall_web"})["company"], "internal")
        self.assertIsNone(healer.resolve_target({"company": "C", "container": "pitwall_web"}))
        self.assertIsNone(healer.resolve_target({"company": "A", "container": "postgres"}))


class StateMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()) / "state.json"
        self.patch = mock.patch.object(healer, "STATE_FILE", self.tmp)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_missing_file(self):
        self.assertEqual(healer.load_state(), {"targets": {}})

    def test_old_single_target_format_is_migrated(self):
        """구버전 상태를 그냥 버리면 열려 있던 서킷이 조용히 풀려 무한 재기동이 가능해진다."""
        self.tmp.write_text(json.dumps({"attempts": [{"container": "pitwall_web", "at": 1.0, "ok": False}],
                                        "circuit_open": True, "opened_at": 2.0}))
        state = healer.load_state()
        migrated = state["targets"]["internal/pitwall_web"]
        self.assertTrue(migrated["circuit_open"])
        self.assertEqual(migrated["opened_at"], 2.0)
        self.assertEqual(len(migrated["attempts"]), 1)


class CircuitIsolationTest(unittest.TestCase):
    """대상별 서킷 — 이 파일의 존재 이유."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()) / "state.json"
        for p in (mock.patch.object(healer, "STATE_FILE", self.tmp),
                  mock.patch.object(healer, "snapshot", return_value=({}, [], 0.0)),
                  mock.patch.object(healer.slack, "post", return_value=False),
                  mock.patch.object(healer, "handoff_to_rca", lambda job: None)):
            p.start()
            self.addCleanup(p.stop)
        # 쿨다운은 이 테스트의 관심사가 아니다
        p = mock.patch.object(healer, "COOLDOWN_SECONDS", 0); p.start(); self.addCleanup(p.stop)

    def _heal(self, company, ok=False):
        with mock.patch.object(healer, "restart_and_verify", return_value=(ok, "stub")):
            return healer.heal({"company": company, "container": "pitwall_web", "event_id": "e1"})

    def test_circuit_opens_per_target_and_other_company_keeps_healing(self):
        for _ in range(healer.MAX_RESTARTS):
            code, _ = self._heal("A")
            self.assertEqual(code, 502)          # 재기동은 됐지만 healthy 확인 실패

        code, body = self._heal("A")             # 한도 초과 → A 만 차단
        self.assertEqual(code, 429)
        self.assertIn("A/pitwall_web", body["reason"])

        code, _ = self._heal("B", ok=True)       # B 는 영향 없이 복구되어야 한다
        self.assertEqual(code, 200)

        state = healer.load_state()["targets"]
        self.assertTrue(state["A/pitwall_web"]["circuit_open"])
        self.assertFalse(state["B/pitwall_web"]["circuit_open"])

    def test_unknown_target_is_rejected(self):
        code, body = self._heal("C")
        self.assertEqual(code, 403)
        self.assertEqual(body["result"], "rejected")

    def test_scoped_reset_clears_only_one_target(self):
        for _ in range(healer.MAX_RESTARTS + 1):
            self._heal("A")
        for _ in range(healer.MAX_RESTARTS + 1):
            self._heal("B")
        self.assertEqual(self._heal("A")[0], 429)

        code, body = healer.reset({"company": "A", "container": "pitwall_web"})
        self.assertEqual((code, body["was_open"]), (200, True))
        state = healer.load_state()["targets"]
        self.assertFalse(state["A/pitwall_web"]["circuit_open"])
        self.assertTrue(state["B/pitwall_web"]["circuit_open"])   # B 는 그대로

    def test_reset_without_body_clears_all(self):
        """measure_detection.py 가 본문 없이 호출한다 — 전체 해제가 유지되어야 한다."""
        for _ in range(healer.MAX_RESTARTS + 1):
            self._heal("A")
        code, body = healer.reset()
        self.assertEqual(code, 200)
        self.assertEqual(body["was_open"], ["A/pitwall_web"])
        self.assertEqual(healer.load_state(), {"targets": {}})

    def test_unknown_target_reset_is_404(self):
        self.assertEqual(healer.reset({"company": "C", "container": "pitwall_web"})[0], 404)

    def test_cooldown_blocks_duplicate_request(self):
        with mock.patch.object(healer, "COOLDOWN_SECONDS", 60):
            self.assertEqual(self._heal("A", ok=True)[0], 200)
            self.assertEqual(self._heal("A", ok=True)[0], 409)


if __name__ == "__main__":
    unittest.main()
