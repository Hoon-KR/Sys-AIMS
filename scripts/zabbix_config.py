#!/usr/bin/env python3
"""Sys-AIMS Zabbix 설정 관리 (Zabbix API 전용, 웹 UI 수동 설정 금지).

  apply   현재 Zabbix에 설정을 적용한다 (반복 실행해도 결과 동일)
  export  zabbix/templates/ 로 내보낸다 (커밋 대상)
  import  export 파일을 가져와 새 환경(EC2 등)에 재현한다

인증: 환경변수 또는 .env 의 ZABBIX_API_URL / ZABBIX_API_USER / ZABBIX_API_PASSWORD
비밀값: .env 의 HEALER_TOKEN / SLACK_WEBHOOK_URL 을 Secret 전역 매크로로 주입한다.
        전역 매크로는 configuration.export 대상이 아니므로 export 파일에 비밀값이 남지 않는다.
"""

import argparse
import json
import pathlib
import secrets
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "automation"))

from common.zabbix_api import ZabbixAPI, load_env  # noqa: E402

EXPORT_DIR = REPO_ROOT / "zabbix" / "templates"
TEMPLATE_FILE = EXPORT_DIR / "sys-aims-http-service.yaml"
HOSTS_FILE = EXPORT_DIR / "hosts.yaml"
MEDIATYPES_FILE = EXPORT_DIR / "mediatypes.yaml"
# Zabbix configuration.export는 Action/사용자/사용자 그룹을 지원하지 않는다.
# 그래서 이름 기반(ID 없음)으로 정규화한 JSON을 직접 내보내고 가져온다.
AUTOMATION_FILE = EXPORT_DIR / "automation.json"
MEDIATYPE_SCRIPTS = REPO_ROOT / "zabbix" / "mediatypes"

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
    # 트리거 태그 healing 의 값. 호스트에서 off 로 재정의하면 자동 복구 대상에서 빠진다
    # (예: healer 자신 — healer가 자기 자신을 고치려 하면 안 된다).
    {"macro": "{$HEALING.MODE}", "value": "auto", "description": "자동 복구 여부 (auto | off)"},
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
    "comments": "최근 2회 연속 HTTP 200이 아니면 발생 (0 = 연결 실패). healing={$HEALING.MODE} 태그로 자동 복구 여부 결정.",
    "tags": [
        {"tag": "scope", "value": "availability"},
        {"tag": "component", "value": "http"},
        {"tag": "healing", "value": "{$HEALING.MODE}"},
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
        # 딥 헬스체크: 의존 서비스(pitwall_api)까지 응답해야 200 (docs/chaos-scenarios.md)
        "macros": [{"macro": "{$SERVICE.URL}", "value": "http://pitwall_web/healthz"}],
    },
    {
        "host": "pitwall_api",
        "templates": [HTTP_TEMPLATE],
        "interfaces": [],
        "tags": [{"tag": "role", "value": "dependency"}],
        "macros": [
            {"macro": "{$SERVICE.URL}", "value": "http://pitwall_api/status.json"},
            # 자동 복구 대상 아님 — 의존 서비스 장애는 사람이 판단한다 (healer 허용 목록 밖)
            {"macro": "{$HEALING.MODE}", "value": "off"},
        ],
    },
    {
        "host": "healer",
        "templates": [HTTP_TEMPLATE],
        "interfaces": [],
        "tags": [{"tag": "role", "value": "automation"}],
        "macros": [
            {"macro": "{$SERVICE.URL}", "value": "http://healer:8080/health"},
            {"macro": "{$HEALING.MODE}", "value": "off"},  # healer는 자동 복구 대상이 아니다
        ],
    },
    {
        "host": "rca",
        "templates": [HTTP_TEMPLATE],
        "interfaces": [],
        "tags": [{"tag": "role", "value": "automation"}],
        "macros": [
            {"macro": "{$SERVICE.URL}", "value": "http://rca:8081/health"},
            {"macro": "{$HEALING.MODE}", "value": "off"},  # rca 장애는 사람이 본다 (복구 경로와 분리)
        ],
    },
]

# ---------------------------------------------------------------
# Self-Healing: Media type / 전용 사용자 / Action
# ---------------------------------------------------------------
HEALER_MEDIA = "Sys-AIMS Healer"
SLACK_MEDIA = "Sys-AIMS Slack"
BOT_USERGROUP = "Sys-AIMS Automation"
BOT_USER = "sys-aims-bot"
ACTION_NAME = "Sys-AIMS Self-Healing"

GLOBAL_SECRET_MACROS = {  # 매크로 → .env 키
    "{$HEALER.TOKEN}": "HEALER_TOKEN",
    "{$SLACK.WEBHOOK}": "SLACK_WEBHOOK_URL",
}

MEDIA_TYPES = [
    {
        "name": HEALER_MEDIA,
        "script_file": "healer.js",
        "description": "Zabbix Action → healer POST /heal (docs/self-healing.md)",
        # healer는 재기동 후 healthy 확인까지 최대 30초를 쓴다
        "timeout": "45s",
        # 재시도는 1회: 재시도/차단 정책은 healer의 서킷 브레이커 한 곳에서만 관리한다
        "maxattempts": "1",
        "parameters": [
            {"name": "url", "value": "http://healer:8080/heal"},
            {"name": "token", "value": "{$HEALER.TOKEN}"},
            {"name": "container", "value": "{EVENT.TAGS.container}"},
            {"name": "event_id", "value": "{EVENT.ID}"},
            {"name": "host", "value": "{HOST.HOST}"},
            {"name": "trigger", "value": "{EVENT.NAME}"},
        ],
        "message_templates": [
            {"eventsource": "0", "recovery": "0", "subject": "heal {EVENT.ID}", "message": "{EVENT.NAME}"},
        ],
    },
    {
        "name": SLACK_MEDIA,
        "script_file": "slack.js",
        "description": "Slack Incoming Webhook — 자동 복구 미해소 시 사람 호출 (에스컬레이션)",
        "timeout": "10s",
        "maxattempts": "3",
        "parameters": [
            {"name": "url", "value": "{$SLACK.WEBHOOK}"},
            {"name": "subject", "value": "{ALERT.SUBJECT}"},
            {"name": "message", "value": "{ALERT.MESSAGE}"},
        ],
        "message_templates": [
            {
                "eventsource": "0", "recovery": "0",
                "subject": ":rotating_light: [Sys-AIMS] 자동 복구 후에도 미해소 — 사람 개입 필요",
                "message": "문제: {EVENT.NAME} ({EVENT.SEVERITY})\n"
                           "호스트: {HOST.NAME}\n"
                           "발생: {EVENT.DATE} {EVENT.TIME} (경과 {EVENT.AGE})\n"
                           "상태: {EVENT.OPDATA}\n"
                           "Event ID: {EVENT.ID}\n"
                           "healer 응답/서킷 상태를 확인하세요 (docs/self-healing.md)",
            },
        ],
    },
]

# 이름 기반 선언 — apply 와 import(automation.json) 가 같은 함수로 적용한다
# ---------------------------------------------------------------
# 일일 보고서 전용 읽기 전용 계정 (docs/daily-report.md)
#   Super admin(ZABBIX_API_*)은 프로비저닝 스크립트만 쓴다. reporter 는 이 계정만 가진다.
#   - 역할: UI 접근 없음, API 는 아래 조회 메서드만 허용 (allow list)
#   - 권한: Sys-AIMS 호스트 그룹 읽기
#   - 비밀번호: .env ZABBIX_REPORT_PASSWORD (export 파일에는 남지 않는다)
# ---------------------------------------------------------------
REPORT_ROLE = "Sys-AIMS Report (read-only)"
REPORT_USERGROUP = "Sys-AIMS Read-only"
REPORT_API_METHODS = ["event.get", "history.get", "host.get", "item.get", "problem.get", "trend.get", "user.logout"]  # logout: 세션 누적 방지
REPORT_ACCESS = {
    "role": {"name": REPORT_ROLE, "type": "1", "api_mode": "1", "api": REPORT_API_METHODS},
    "usergroup": {"name": REPORT_USERGROUP, "gui_access": "3", "hostgroup_rights": [{"hostgroup": HOST_GROUP, "permission": "2"}]},
    "user": {"username_env": "ZABBIX_REPORT_USER", "role": REPORT_ROLE, "usergroups": [REPORT_USERGROUP]},
}

AUTOMATION = {
    "usergroup": {"name": BOT_USERGROUP, "gui_access": "3", "hostgroup_rights": [{"hostgroup": HOST_GROUP, "permission": "2"}]},
    "user": {"username": BOT_USER, "role": "User role", "usergroups": [BOT_USERGROUP],
             "medias": [{"mediatype": HEALER_MEDIA, "sendto": "healer"}, {"mediatype": SLACK_MEDIA, "sendto": "slack"}]},
    "action": {
        "name": ACTION_NAME,
        "eventsource": "0",
        "status": "0",
        # 1단계(즉시) → healer 호출, 5분 뒤에도 문제가 열려 있으면 2단계 → Slack
        "esc_period": "5m",
        # 조건은 트리거 이름이 아니라 태그: healing=auto 인 이벤트만
        "conditions": [{"conditiontype": "26", "operator": "0", "value": "auto", "value2": "healing"}],
        "operations": [
            {"esc_step_from": "1", "esc_step_to": "1", "mediatype": HEALER_MEDIA, "users": [BOT_USER]},
            {"esc_step_from": "2", "esc_step_to": "2", "mediatype": SLACK_MEDIA, "users": [BOT_USER]},
        ],
    },
}


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


def ensure_global_secret_macros(api):
    env = load_env()
    for macro, env_key in GLOBAL_SECRET_MACROS.items():
        value = env.get(env_key, "")
        if not value:
            log(f"WARN: {env_key} is empty — {macro} not set")
            continue
        found = api.call("usermacro.get", {"globalmacro": True, "filter": {"macro": macro}, "output": ["globalmacroid"]})
        params = {"value": value, "type": 1, "description": f"from .env {env_key} (secret)"}
        if found:
            api.call("usermacro.updateglobal", {"globalmacroid": found[0]["globalmacroid"], **params})
        else:
            log(f"create secret global macro {macro}")
            api.call("usermacro.createglobal", {"macro": macro, **params})


def ensure_media_types(api):
    for spec in MEDIA_TYPES:
        params = {k: v for k, v in spec.items() if k != "script_file"}
        params.update({"type": 4, "script": (MEDIATYPE_SCRIPTS / spec["script_file"]).read_text().rstrip(), "status": 0})
        found = api.call("mediatype.get", {"filter": {"name": spec["name"]}, "output": ["mediatypeid"]})
        if found:
            api.call("mediatype.update", {"mediatypeid": found[0]["mediatypeid"], **params})
        else:
            log(f"create media type '{spec['name']}'")
            api.call("mediatype.create", params)


def _id(api, method, field, name, key):
    found = api.call(f"{method}.get", {"filter": {field: [name]}, "output": [key]})
    if not found:
        raise SystemExit(f"{method} '{name}' not found")
    return found[0][key]


def ensure_automation(api, spec):
    ug = spec["usergroup"]
    ug_params = {"gui_access": ug["gui_access"],
                 "hostgroup_rights": [{"id": _id(api, "hostgroup", "name", r["hostgroup"], "groupid"), "permission": r["permission"]}
                                      for r in ug["hostgroup_rights"]]}
    found = api.call("usergroup.get", {"filter": {"name": ug["name"]}, "output": ["usrgrpid"]})
    if found:
        api.call("usergroup.update", {"usrgrpid": found[0]["usrgrpid"], **ug_params})
    else:
        log(f"create usergroup '{ug['name']}'")
        api.call("usergroup.create", {"name": ug["name"], **ug_params})

    u = spec["user"]
    u_params = {
        "roleid": _id(api, "role", "name", u["role"], "roleid"),
        "usrgrps": [{"usrgrpid": _id(api, "usergroup", "name", g, "usrgrpid")} for g in u["usergroups"]],
        "medias": [{"mediatypeid": _id(api, "mediatype", "name", m["mediatype"], "mediatypeid"), "sendto": m["sendto"],
                    "active": 0, "severity": 63, "period": "1-7,00:00-24:00"} for m in u["medias"]],
    }
    found = api.call("user.get", {"filter": {"username": u["username"]}, "output": ["userid"]})
    if found:
        api.call("user.update", {"userid": found[0]["userid"], **u_params})
    else:
        # 로그인하지 않는 알림 전용 계정 (GUI 접근 비활성). 비밀번호는 저장하지 않는다.
        log(f"create user '{u['username']}'")
        api.call("user.create", {"username": u["username"], "passwd": secrets.token_urlsafe(32), **u_params})

    a = spec["action"]
    a_params = {
        "status": a["status"],
        "esc_period": a["esc_period"],
        "filter": {"evaltype": "0", "conditions": a["conditions"]},
        "operations": [{
            "operationtype": "0", "esc_step_from": op["esc_step_from"], "esc_step_to": op["esc_step_to"], "esc_period": "0",
            "opmessage": {"default_msg": "1", "mediatypeid": _id(api, "mediatype", "name", op["mediatype"], "mediatypeid")},
            "opmessage_usr": [{"userid": _id(api, "user", "username", name, "userid")} for name in op["users"]],
        } for op in a["operations"]],
    }
    found = api.call("action.get", {"filter": {"name": a["name"]}, "output": ["actionid"]})
    if found:
        api.call("action.update", {"actionid": found[0]["actionid"], **a_params})
    else:
        log(f"create action '{a['name']}'")
        api.call("action.create", {"name": a["name"], "eventsource": a["eventsource"], **a_params})


def ensure_report_access(api, spec):
    env = load_env()
    username, password = env.get(spec["user"]["username_env"], ""), env.get("ZABBIX_REPORT_PASSWORD", "")
    if not username or not password:
        log("WARN: ZABBIX_REPORT_USER / ZABBIX_REPORT_PASSWORD empty — report account not configured")
        return
    r = spec["role"]
    # UI 는 전부 거부(ui.default_access=0), API 는 허용 목록만(api.mode=1)
    rules = {"ui.default_access": 0, "actions.default_access": 0, "modules.default_access": 0,
             "api.access": 1, "api.mode": int(r["api_mode"]), "api": r["api"]}
    found = api.call("role.get", {"filter": {"name": r["name"]}, "output": ["roleid"]})
    if found:
        roleid = found[0]["roleid"]
        api.call("role.update", {"roleid": roleid, "rules": rules})
    else:
        log(f"create role '{r['name']}'")
        roleid = api.call("role.create", {"name": r["name"], "type": int(r["type"]), "rules": rules})["roleids"][0]

    ug = spec["usergroup"]
    ug_params = {"gui_access": ug["gui_access"],
                 "hostgroup_rights": [{"id": _id(api, "hostgroup", "name", x["hostgroup"], "groupid"), "permission": x["permission"]}
                                      for x in ug["hostgroup_rights"]]}
    found = api.call("usergroup.get", {"filter": {"name": ug["name"]}, "output": ["usrgrpid"]})
    if found:
        api.call("usergroup.update", {"usrgrpid": found[0]["usrgrpid"], **ug_params})
    else:
        log(f"create usergroup '{ug['name']}'")
        api.call("usergroup.create", {"name": ug["name"], **ug_params})

    u_params = {"roleid": roleid, "passwd": password,
                "usrgrps": [{"usrgrpid": _id(api, "usergroup", "name", g, "usrgrpid")} for g in spec["user"]["usergroups"]]}
    found = api.call("user.get", {"filter": {"username": username}, "output": ["userid"]})
    if found:
        api.call("user.update", {"userid": found[0]["userid"], **u_params})
    else:
        log(f"create user '{username}' (read-only)")
        api.call("user.create", {"username": username, **u_params})


def export_report_access(api):
    r = api.call("role.get", {"filter": {"name": REPORT_ROLE}, "output": ["name", "type"], "selectRules": ["api.mode", "api"]})[0]
    ug = api.call("usergroup.get", {"filter": {"name": REPORT_USERGROUP}, "output": ["name", "gui_access"], "selectHostGroupRights": "extend"})[0]
    groups = {g["groupid"]: g["name"] for g in api.call("hostgroup.get", {"groupids": [x["id"] for x in ug["hostgroup_rights"]], "output": ["groupid", "name"]})}
    return {
        "role": {"name": r["name"], "type": r["type"], "api_mode": str(r["rules"]["api.mode"]), "api": sorted(r["rules"]["api"])},
        "usergroup": {"name": ug["name"], "gui_access": ug["gui_access"],
                      "hostgroup_rights": [{"hostgroup": groups[x["id"]], "permission": x["permission"]} for x in ug["hostgroup_rights"]]},
        "user": {"username_env": "ZABBIX_REPORT_USER", "role": r["name"], "usergroups": [ug["name"]]},
    }


def export_automation(api):
    """live 상태를 이름 기반으로 정규화해 AUTOMATION 과 같은 형태로 만든다."""
    names = lambda method, key, field, ids: {x[key]: x[field] for x in api.call(f"{method}.get", {f"{key}s": ids, "output": [key, field]})} if ids else {}
    ug = api.call("usergroup.get", {"filter": {"name": BOT_USERGROUP}, "output": ["name", "gui_access"], "selectHostGroupRights": "extend"})[0]
    groups = names("hostgroup", "groupid", "name", [r["id"] for r in ug["hostgroup_rights"]])
    u = api.call("user.get", {"filter": {"username": BOT_USER}, "output": ["username"], "selectRole": ["name"],
                              "selectUsrgrps": ["name"], "selectMedias": ["mediatypeid", "sendto"]})[0]
    a = api.call("action.get", {"filter": {"name": ACTION_NAME}, "output": ["name", "eventsource", "status", "esc_period"],
                                "selectFilter": "extend", "selectOperations": "extend"})[0]
    mt_ids = [m["mediatypeid"] for m in u["medias"]] + [op["opmessage"]["mediatypeid"] for op in a["operations"]]
    media = names("mediatype", "mediatypeid", "name", mt_ids)
    users = names("user", "userid", "username", [x["userid"] for op in a["operations"] for x in op["opmessage_usr"]])
    sendto = lambda s: s if isinstance(s, str) else s[0]
    return {
        "usergroup": {"name": ug["name"], "gui_access": ug["gui_access"],
                      "hostgroup_rights": [{"hostgroup": groups[r["id"]], "permission": r["permission"]} for r in ug["hostgroup_rights"]]},
        "user": {"username": u["username"], "role": u["role"]["name"], "usergroups": [g["name"] for g in u["usrgrps"]],
                 "medias": [{"mediatype": media[m["mediatypeid"]], "sendto": sendto(m["sendto"])} for m in u["medias"]]},
        "action": {
            "name": a["name"], "eventsource": a["eventsource"], "status": a["status"], "esc_period": a["esc_period"],
            "conditions": [{k: c[k] for k in ("conditiontype", "operator", "value", "value2")} for c in a["filter"]["conditions"]],
            "operations": [{"esc_step_from": op["esc_step_from"], "esc_step_to": op["esc_step_to"],
                            "mediatype": media[op["opmessage"]["mediatypeid"]],
                            "users": [users[x["userid"]] for x in op["opmessage_usr"]]}
                           for op in sorted(a["operations"], key=lambda o: int(o["esc_step_from"]))],
        },
    }


def cmd_apply(api):
    ensure_http_template(api)
    groupid = ensure_group(api, "hostgroup", HOST_GROUP)
    for spec in HOSTS:
        ensure_host(api, spec, groupid)
    fix_default_server_host(api)
    ensure_global_secret_macros(api)
    ensure_media_types(api)
    ensure_automation(api, AUTOMATION)
    ensure_report_access(api, REPORT_ACCESS)


def cmd_export(api):
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    tid = template_id(api, HTTP_TEMPLATE)
    TEMPLATE_FILE.write_text(api.call("configuration.export", {"format": "yaml", "options": {"templates": [tid]}}))
    hosts = api.call("host.get", {"filter": {"host": [h["host"] for h in HOSTS]}, "output": ["hostid"]})
    HOSTS_FILE.write_text(api.call("configuration.export", {"format": "yaml", "options": {"hosts": [h["hostid"] for h in hosts]}}))
    media = api.call("mediatype.get", {"filter": {"name": [m["name"] for m in MEDIA_TYPES]}, "output": ["mediatypeid"]})
    MEDIATYPES_FILE.write_text(api.call("configuration.export", {"format": "yaml", "options": {"mediaTypes": [m["mediatypeid"] for m in media]}}))
    AUTOMATION_FILE.write_text(json.dumps({**export_automation(api), "report_access": export_report_access(api)},
                                          ensure_ascii=False, indent=2) + "\n")
    for path in (TEMPLATE_FILE, HOSTS_FILE, MEDIATYPES_FILE, AUTOMATION_FILE):
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
    "mediaTypes": {"createMissing": True, "updateExisting": True},
}


def cmd_import(api):
    # 템플릿이 먼저 있어야 호스트의 템플릿 연결이 성공한다
    for path in (TEMPLATE_FILE, HOSTS_FILE, MEDIATYPES_FILE):
        log(f"import {path.relative_to(REPO_ROOT)}")
        # 새로 설치한 Zabbix에서 템플릿 연결(아이템 약 150개 생성)은 15초를 넘길 수 있다 (troubleshooting #9)
        api.call("configuration.import", {"format": "yaml", "rules": IMPORT_RULES, "source": path.read_text()}, timeout=120)
    fix_default_server_host(api)
    ensure_global_secret_macros(api)
    log(f"import {AUTOMATION_FILE.relative_to(REPO_ROOT)}")
    automation = json.loads(AUTOMATION_FILE.read_text())
    ensure_automation(api, automation)
    ensure_report_access(api, automation.get("report_access", REPORT_ACCESS))


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
