"""하루 호출 횟수 상한 (비용 통제). rca / daily_report 공용.

상태 파일에 {"date": "YYYY-MM-DD", "count": N} 을 저장한다. 날짜가 바뀌면 0부터 다시 센다.
재시작해도 카운트가 유지되도록 볼륨에 둔다.
"""

import json
import pathlib
import time


def take(state_file, limit):
    """오늘 한도 안이면 카운트를 올리고 (True, 사용량), 아니면 (False, 사용량) 을 돌려준다."""
    state_file = pathlib.Path(state_file)
    today = time.strftime("%Y-%m-%d")
    try:
        state = json.loads(state_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    if state.get("date") != today:
        state = {"date": today, "count": 0}
    if state["count"] >= limit:
        return False, state["count"]
    state["count"] += 1
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(state_file)
    return True, state["count"]
