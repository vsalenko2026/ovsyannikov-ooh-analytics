"""Поддельный транспорт вместо requests.Session — для тестов без сети."""

from __future__ import annotations

import datetime as dt
import json


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else json.dumps(payload or {}, ensure_ascii=False)
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("не JSON")
        return self._payload


class FakeSession:
    """Отдаёт заранее заготовленные ответы и запоминает запросы."""

    def __init__(self, responses=None, generator=None):
        self.responses = list(responses or [])
        self.generator = generator
        self.requests = []

    def post(self, url, data=None, headers=None, timeout=None):
        body = json.loads(data.decode("utf-8"))
        self.requests.append({"url": url, "body": body, "headers": headers})
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(payload=self.generator(body))


def weekly_series(body, base=10, step=3, anchor="start"):
    """Синтетический ряд GetDynamics: count строкой, как у настоящего API."""
    start = dt.date.fromisoformat(body["fromDate"][:10])
    end = dt.date.fromisoformat(body["toDate"][:10])
    start -= dt.timedelta(days=start.weekday())
    results = []
    week, index = start, 0
    while week <= end:
        stamp = week if anchor == "start" else week + dt.timedelta(days=6)
        results.append(
            {
                "date": f"{stamp.isoformat()}T00:00:00Z",
                "count": str(base + step * index),
                "share": round(0.0001 * (base + step * index), 8),
            }
        )
        week += dt.timedelta(days=7)
        index += 1
    return {"results": results}


def daily_series(body, base=5, step=1):
    """Синтетический ряд по дням."""
    start = dt.date.fromisoformat(body["fromDate"][:10])
    end = dt.date.fromisoformat(body["toDate"][:10])
    results, day, index = [], start, 0
    while day <= end:
        results.append(
            {"date": f"{day.isoformat()}T00:00:00Z", "count": str(base + step * index),
             "share": 0.0001}
        )
        day += dt.timedelta(days=1)
        index += 1
    return {"results": results}


def top_payload(body, nested=3, associations=2):
    """Синтетический ответ GetTop."""
    phrase = body["phrase"]
    return {
        "totalCount": "1000",
        "results": [
            {"phrase": f"{phrase} {i}", "count": str(100 - 10 * i)} for i in range(nested)
        ],
        "associations": [
            {"phrase": f"похожий {i}", "count": str(50 - 5 * i)} for i in range(associations)
        ],
    }


def dispatch(body):
    """Ответ по форме запроса: GetDynamics или GetTop."""
    if "period" not in body:
        return top_payload(body)
    if body["period"] == "PERIOD_DAILY":
        return daily_series(body)
    return weekly_series(body)
