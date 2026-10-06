"""common.redact 테스트 — 가려야 할 것과 **건드리면 안 되는 것** 둘 다 고정한다.

실행: python3 -m unittest discover -s automation/tests -t automation
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common.redact import redact  # noqa: E402


class TestRedactsSensitive(unittest.TestCase):
    def test_private_ip_becomes_stable_pseudonym(self):
        out = redact("client 172.31.9.142 retry 172.31.9.142 then 172.31.9.33")
        self.assertNotIn("172.31.9.142", out)
        self.assertNotIn("172.31.9.33", out)
        # 같은 IP → 같은 가명, 다른 IP → 다른 가명 (반복 요청 추론을 살린다)
        self.assertEqual(out, "client <ip-1> retry <ip-1> then <ip-2>")

    def test_public_ip_redacted(self):
        self.assertEqual(redact("from 43.201.27.97"), "from <ip-1>")

    def test_ec2_internal_hostname_before_ip_rule(self):
        out = redact("host ip-172-31-45-141.ap-northeast-2.compute.internal down")
        self.assertEqual(out, "host <host-1> down")
        self.assertNotIn("172-31-45-141", out)

    def test_email_phone_rrn(self):
        out = redact("user a.b+x@example.co.kr 010-1234-5678 900101-1234567")
        self.assertIn("[REDACTED-EMAIL]", out)
        self.assertIn("[REDACTED-PHONE]", out)
        self.assertIn("[REDACTED-RRN]", out)
        self.assertNotIn("example.co.kr", out)

    def test_card_number_only_when_luhn_passes(self):
        self.assertIn("[REDACTED-CARD]", redact("card 4242-4242-4242-4242"))
        self.assertIn("[REDACTED-CARD]", redact("card 4242424242424242"))

    def test_secrets(self):
        out = redact("Authorization: Bearer abcdef0123456789 key=sk-abcdefghijklmnopqrst")
        self.assertIn("Bearer [REDACTED]", out)
        self.assertIn("sk-[REDACTED]", out)
        self.assertNotIn("abcdefghijklmnopqrst", out)

    def test_aws_identifiers(self):
        self.assertIn("i-[REDACTED]", redact("instance i-07cee77798ac03c04 stopped"))

    def test_slack_webhook(self):
        self.assertNotIn("T00000", redact("https://hooks.slack.com/services/T00000/B00000/xxxx"))


class TestKeepsDiagnostics(unittest.TestCase):
    """가리면 RCA 정확도가 떨어지는 것들. 오탐 방지가 이 테스트의 목적이다."""

    def test_service_names_kept(self):
        line = 'pitwall_web restart failed: pitwall_api unreachable'
        self.assertEqual(redact(line), line)

    def test_loopback_and_docker_dns_kept(self):
        line = "proxy 127.0.0.1:8080 resolver 127.0.0.11 bind 0.0.0.0:443"
        self.assertEqual(redact(line), line)

    def test_status_port_bytes_version_kept(self):
        line = 'GET /healthz HTTP/1.1" 502 1234567 nginx/1.30.5 upstream timeout 60s'
        self.assertEqual(redact(line), line)

    def test_long_byte_counts_are_not_cards(self):
        # Luhn 을 통과하지 못하는 긴 숫자는 그대로 둔다
        line = "sent 1234567890123 bytes in 1760000000000 ms"
        self.assertEqual(redact(line), line)

    def test_version_like_dotted_numbers_kept(self):
        line = "api version 1.44 engine 29.8.2 containerd v2.3.6"
        self.assertEqual(redact(line), line)

    def test_empty_input(self):
        self.assertEqual(redact(""), "")
        self.assertIsNone(redact(None))


if __name__ == "__main__":
    unittest.main()
