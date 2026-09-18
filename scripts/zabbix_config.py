#!/usr/bin/env python3
"""Sys-AIMS Zabbix 설정 관리 (Zabbix API 전용, 웹 UI 수동 설정 금지).

  apply   현재 Zabbix에 설정을 적용한다 (반복 실행해도 결과 동일)
  export  zabbix/templates/*.yaml 로 내보낸다 (커밋 대상)
  import  YAML을 가져와 새 환경(EC2 등)에 재현한다

인증: 환경변수 또는 .env 의 ZABBIX_API_URL / ZABBIX_API_USER / ZABBIX_API_PASSWORD
"""

import argparse
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "automation"))

from common.zabbix_api import ZabbixAPI  # noqa: E402

EXPORT_DIR = REPO_ROOT / "zabbix" / "templates"
TEMPLATE_FILE = EXPORT_DIR / "sys-aims-http-service.yaml"
HOSTS_FILE = EXPORT_DIR / "hosts.yaml"

HOST_GROUP = "Sys-AIMS"
TEMPLATE_GROUP = "Templates/Sys-AIMS"
HTTP_TEMPLATE = "Sys-AIMS HTTP Service"
LINUX_TEMPLATE = "Linux by Zabbix agent"
DEFAULT_SERVER_HOST = "Zabbix server"

# ---------------------------------------------------------------
# 감시 정책 (근거: docs/zabbix-monitoring.md)
#   15초 주기 × 연속 2회 실패 → 이론상 감지 15~35초
#   1회 실패로 판정하지 않는 이유: 이 트리거는 자동 재기동으로 이어지므로
#   순간 지연(오탐)으로 멀쩡한 서비스를 재시작하면 안 된다.
# ---------------------------------------------------------------
HTTP_KEY = "http.status.code"
TEMPLATE_MACROS = [
    {"macro": "{$SERVICE.URL}", "value": "http://localhost/", "description": "감시 대상 URL (호스트에서 재정의)"},
    {"macro": "{$HTTP.CHECK.INTERVAL}", "value": "15s", "description": "HTTP 체크 주기"},
    {"macro": "{$HTTP.CHECK.TIMEOUT}", "value": "5s", "description": "HTTP 응답 타임아웃"},
]
HTTP_ITEM = {
    "name": "HTTP status code",
    "key_": HTTP_KEY,
    "type": 19,  # HTTP agent
    "value_type": 3,  # numeric unsigned
    "url": "{$SERVICE.URL}",
    "delay": "{$HTTP.CHECK.INTERVAL}",
    "timeout": "{$HTTP.CHECK.TIMEOUT}",
    "retrieve_mode": 1,  # headers only
    "follow_redirects": 0,
    "status_codes": "",  # 어떤 코드든 값으로 수집 (판정은 트리거가 한다)
    "history": "7d",
    "trends": "90d",
    "description": "0 = 연결 실패/타임아웃, 그 외 = HTTP 상태 코드",
    "tags": [{"tag": "component", "value": "http"}],
    "preprocessing": [
        # 연결 거부/타임아웃으로 아이템이 오류가 되면 0으로 치환
        {"type": 26, "params": "-1", "error_handler": 2, "error_handler_params": "0"},
        # 응답 헤더의 상태 줄에서 코드만 추출.
        # 위 단계에서 0이 된 값도 이 단계를 거치므로 매칭 실패 시 0을 유지한다
        # (해석 불가한 응답 역시 장애로 본다).
        {"type": 5, "params": "HTTP\\/[\\d.]+\\s+(\\d{3})\n\\1", "error_handler": 2, "error_handler_params": "0"},
    ],
}
HTTP_TRIGGER = {
    # Action은 이름이 아니라 태그(healing: auto)로 매칭한다
    "description": "{HOST.NAME}: HTTP service is down",
    "expression": f'count(/{HTTP_TEMPLATE}/{HTTP_KEY},#2,"ne","200")=2',
    "recovery_mode": 1,  # recovery expression
    "recovery_expression": f"last(/{HTTP_TEMPLATE}/{HTTP_KEY})=200",
    "priority": 4,  # High
    "manual_close": 1,
    "opdata": "Status: {ITEM.LASTVALUE1}",
    "comments": "최근 2회 연속 HTTP 200이 아니면 발생 (0 = 연결 실패). healing:auto 태그로 자동 복구 대상.",
    "tags": [
        {"tag": "scope", "value": "availability"},
        {"tag": "component", "value": "http"},
        {"tag": "healing", "value": "auto"},
    ],
}

HOSTS = [
    {
        "host": "zabbix-agent",  # compose의 ZBX_HOSTNAME과 일치해야 한다
        "templates": [LINUX_TEMPLATE],
        # IP가 아닌 DNS 이름으로 연결 (컨테이너 재생성 시 IP가 바뀔 수 있음)
        "interfaces": [{"type": 1, "main": 1, "useip": 0, "ip": "", "dns": "zabbix-agent", "port": "10050"}],
        "tags": [{"tag": "role", "value": "host-os"}],
        "macros": [],
    },
    {
        "host": "pitwall_web",
        "templates": [HTTP_TEMPLATE],
        "interfaces": [],  # HTTP agent는 인터페이스가 필요 없다
        # 이벤트에 전파되어 healer가 재기동할 컨테이너를 식별하는 데 쓴다
        "tags": [{"tag": "container", "value": "pitwall_web"}],
        "macros": [{"macro": "{$SERVICE.URL}", "value": "http://pitwall_web/"}],
    },
]


def log(msg):
    print(f"  {msg}")


def ensure_group(api, kind, name):
    found = api.call(f"{kind}.get", {"filter": {"name": [name]}, "output": ["groupid"]})
    if found:
        return found[0]["groupid"]
    log(f"create {kind} '{name}'")
    return api.call(f"{kind}.create", {"name": name})["groupids"][0]


def template_id(api, name):
    found = api.call("template.get", {"filter": {"host": [name]}, "output": ["templateid"]})
    return found[0]["templateid"] if found else None


def ensure_http_template(api):
    groupid = ensure_group(api, "templategroup", TEMPLATE_GROUP)
    tid = template_id(api, HTTP_TEMPLATE)
    params = {
        "groups": [{"groupid": groupid}],
        "macros": TEMPLATE_MACROS,
        "description": "Sys-AIMS: Docker 소켓 없이 HTTP로 서비스 가용성을 감시 (ADR-0001)",
    }
    if tid:
        api.call("template.update", {"templateid": tid, **params})
    else:
        log(f"create template '{HTTP_TEMPLATE}'")
        tid = api.call("template.create", {"host": HTTP_TEMPLATE, **params})["templateids"][0]

    items = api.call("item.get", {"templateids": tid, "filter": {"key_": HTTP_KEY}, "output": ["itemid"]})
    if items:
        api.call("item.update", {"itemid": items[0]["itemid"], **{k: v for k, v in HTTP_ITEM.items() if k not in ("key_", "type", "value_type")}})
    else:
        log(f"create item '{HTTP_KEY}'")
        api.call("item.create", {"hostid": tid, **HTTP_ITEM})

    triggers = api.call("trigger.get", {"templateids": tid, "filter": {"description": HTTP_TRIGGER["description"]}, "output": ["triggerid"]})
    if triggers:
        api.call("trigger.update", {"triggerid": triggers[0]["triggerid"], **HTTP_TRIGGER})
    else:
        log(f"create trigger '{HTTP_TRIGGER['description']}'")
        api.call("trigger.create", HTTP_TRIGGER)
    return tid


def ensure_host(api, spec, groupid):
    templates = [{"templateid": template_id(api, name)} for name in spec["templates"]]
    found = api.call("host.get", {"filter": {"host": [spec["host"]]}, "output": ["hostid"], "selectInterfaces": ["interfaceid"]})
    params = {"groups": [{"groupid": groupid}], "templates": templates, "tags": spec["tags"], "macros": spec["macros"]}
    if found:
        hostid = found[0]["hostid"]
        if spec["interfaces"] and not found[0]["interfaces"]:
            params["interfaces"] = spec["interfaces"]
        api.call("host.update", {"hostid": hostid, **params})
        return hostid
    log(f"create host '{spec['host']}'")
    return api.call("host.create", {"host": spec["host"], "interfaces": spec["interfaces"], **params})["hostids"][0]


def fix_default_server_host(api):
    """기본 호스트 'Zabbix server'는 127.0.0.1:10050의 agent를 바라본다.
    Docker에서는 server 컨테이너 안에 agent가 없어 'Zabbix agent is not available'
    알람이 뜬다 (docs/troubleshooting.md). OS 지표는 'zabbix-agent' 호스트가 담당하므로
    Linux 템플릿을 unlink+clear 하고, 쓰이지 않는 agent 인터페이스를 제거한다.
    'Zabbix server health'(internal 아이템)는 유지한다."""
    found = api.call("host.get", {
        "filter": {"host": [DEFAULT_SERVER_HOST]}, "output": ["hostid"],
        "selectParentTemplates": ["templateid", "host"], "selectInterfaces": ["interfaceid", "type"],
    })
    if not found:
        return
    host = found[0]
    linux = [t for t in host["parentTemplates"] if t["host"] == LINUX_TEMPLATE]
    if linux:
        keep = [{"templateid": t["templateid"]} for t in host["parentTemplates"] if t["host"] != LINUX_TEMPLATE]
        log(f"'{DEFAULT_SERVER_HOST}': unlink+clear '{LINUX_TEMPLATE}'")
        api.call("host.update", {"hostid": host["hostid"], "templates": keep, "templates_clear": [{"templateid": linux[0]["templateid"]}]})
    agent_ifaces = [i["interfaceid"] for i in host["interfaces"] if i["type"] == "1"]
    if agent_ifaces:
        log(f"'{DEFAULT_SERVER_HOST}': remove unused agent interface")
        api.call("hostinterface.delete", agent_ifaces)


def cmd_apply(api):
    ensure_http_template(api)
    groupid = ensure_group(api, "hostgroup", HOST_GROUP)
    for spec in HOSTS:
        ensure_host(api, spec, groupid)
    fix_default_server_host(api)


def cmd_export(api):
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    tid = template_id(api, HTTP_TEMPLATE)
    TEMPLATE_FILE.write_text(api.call("configuration.export", {"format": "yaml", "options": {"templates": [tid]}}))
    hosts = api.call("host.get", {"filter": {"host": [h["host"] for h in HOSTS]}, "output": ["hostid"]})
    HOSTS_FILE.write_text(api.call("configuration.export", {"format": "yaml", "options": {"hosts": [h["hostid"] for h in hosts]}}))
    for path in (TEMPLATE_FILE, HOSTS_FILE):
        log(f"wrote {path.relative_to(REPO_ROOT)}")


IMPORT_RULES = {
    "template_groups": {"createMissing": True, "updateExisting": True},
    "host_groups": {"createMissing": True, "updateExisting": True},
    "templates": {"createMissing": True, "updateExisting": True},
    "hosts": {"createMissing": True, "updateExisting": True},
    "items": {"createMissing": True, "updateExisting": True, "deleteMissing": True},
    "triggers": {"createMissing": True, "updateExisting": True, "deleteMissing": True},
    "templateLinkage": {"createMissing": True},
    "valueMaps": {"createMissing": True, "updateExisting": True},
}


def cmd_import(api):
    # 템플릿이 먼저 있어야 호스트의 템플릿 연결이 성공한다
    for path in (TEMPLATE_FILE, HOSTS_FILE):
        log(f"import {path.relative_to(REPO_ROOT)}")
        api.call("configuration.import", {"format": "yaml", "rules": IMPORT_RULES, "source": path.read_text()})
    fix_default_server_host(api)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["apply", "export", "import"])
    args = parser.parse_args()
    with ZabbixAPI.from_env() as api:
        print(f"[{args.command}] {api.url}")
        {"apply": cmd_apply, "export": cmd_export, "import": cmd_import}[args.command](api)
    print("done")


if __name__ == "__main__":
    main()
