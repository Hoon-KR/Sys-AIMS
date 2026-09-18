// Sys-AIMS Slack webhook — Incoming Webhook으로 알림 전송
// 파라미터: url({$SLACK.WEBHOOK}), subject({ALERT.SUBJECT}), message({ALERT.MESSAGE})
// healer가 죽었을 때도 사람에게 알릴 수 있도록 Zabbix가 직접 보낸다 (에스컬레이션 2단계).
var params = JSON.parse(value);

var request = new HttpRequest();
request.addHeader('Content-Type: application/json');

var response = request.post(params.url, JSON.stringify({ text: '*' + params.subject + '*\n' + params.message }));
var status = request.getStatus();

if (status !== 200) {
    throw 'Slack HTTP ' + status + ': ' + response;
}
return 'OK';
