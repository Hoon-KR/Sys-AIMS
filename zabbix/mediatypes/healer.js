// Sys-AIMS Healer webhook — Zabbix Action → healer POST /heal
// 파라미터: url, token({$HEALER.TOKEN}), container({EVENT.TAGS.container}), event_id, host, trigger
// healer가 2xx가 아니면 예외를 던져 Action 상태를 Failed로 남긴다 (이벤트 화면에 오류 표시).
var params = JSON.parse(value);

if (!params.container || params.container.indexOf('{') === 0) {
    throw 'event has no "container" tag — nothing to heal';
}

var request = new HttpRequest();
request.addHeader('Content-Type: application/json');
request.addHeader('Authorization: Bearer ' + params.token);

var body = JSON.stringify({
    container: params.container,
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
