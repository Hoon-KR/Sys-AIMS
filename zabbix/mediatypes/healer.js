// Sys-AIMS Healer webhook — Zabbix Action → healer POST /heal
// 파라미터: url, token({$HEALER.TOKEN}), container({EVENT.TAGS.container}),
//          company({EVENT.TAGS.company}), event_id, host, trigger
// healer가 2xx가 아니면 예외를 던져 Action 상태를 Failed로 남긴다 (이벤트 화면에 오류 표시).
var params = JSON.parse(value);

if (!params.container || params.container.indexOf('{') === 0) {
    throw 'event has no "container" tag — nothing to heal';
}

// 태그가 없으면 Zabbix 가 매크로를 그대로 남긴다("{EVENT.TAGS.company}").
// 그 경우 company 를 비워 보낸다 — healer 가 대상이 여럿일 때 거부한다(fail closed).
// 추측해서 보내면 태그가 빠진 VM 장애로 감시 서버의 컨테이너를 재기동할 수 있다.
var company = params.company;
if (!company || company.indexOf('{') === 0) {
    company = '';
}

var request = new HttpRequest();
request.addHeader('Content-Type: application/json');
request.addHeader('Authorization: Bearer ' + params.token);

var body = JSON.stringify({
    container: params.container,
    company: company,
    event_id: params.event_id,
    host: params.host,
    trigger: params.trigger
});

var response = request.post(params.url, body);
var status = request.getStatus();
Zabbix.log(4, '[Sys-AIMS Healer] HTTP ' + status + ' ' + response);

if (status < 200 || status >= 300) {
    throw 'healer HTTP ' + status + ': ' + response;
}
return response;
