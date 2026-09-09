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
