"""최소 Zabbix JSON-RPC 클라이언트 (표준 라이브러리만 사용).

인증 정보는 환경변수 → .env 순으로 읽는다. 값은 절대 출력하지 않는다.
"""

import json
import os
import pathlib
import urllib.request

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def load_env(path=REPO_ROOT / ".env"):
    """.env를 읽되, 이미 설정된 환경변수가 우선한다."""
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    values.update({k: v for k, v in os.environ.items() if k in values or k.startswith("ZABBIX_")})
    return values


class ZabbixAPIError(RuntimeError):
    pass


class ZabbixAPI:
    def __init__(self, url, username, password, timeout=15):
        self.url = url
        self.timeout = timeout
        self._auth = None
        self._auth = self.call("user.login", {"username": username, "password": password})

    @classmethod
    def from_env(cls):
        env = load_env()
        missing = [k for k in ("ZABBIX_API_URL", "ZABBIX_API_USER", "ZABBIX_API_PASSWORD") if not env.get(k)]
        if missing:
            raise ZabbixAPIError(f"missing env: {', '.join(missing)}")
        return cls(env["ZABBIX_API_URL"], env["ZABBIX_API_USER"], env["ZABBIX_API_PASSWORD"])

    def call(self, method, params=None):
        headers = {"Content-Type": "application/json-rpc"}
        if self._auth:
            headers["Authorization"] = f"Bearer {self._auth}"
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1}).encode()
        request = urllib.request.Request(self.url, body, headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            result = json.load(response)
        if "error" in result:
            error = result["error"]
            raise ZabbixAPIError(f"{method}: {error.get('message')} {error.get('data')}")
        return result["result"]

    def logout(self):
        if self._auth:
            self.call("user.logout", [])
            self._auth = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.logout()
