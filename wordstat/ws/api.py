"""Клиент Wordstat API (Yandex Search API v2, сервис AI Studio).

Эндпоинты:
    POST /v2/wordstat/dynamics        GetDynamics       — частотность по неделям
    POST /v2/wordstat/getRegionsTree  GetRegionsTree    — справочник регионов
    POST /v2/wordstat/topRequests     GetTop            — топ вложенных запросов

Авторизация — заголовок `Authorization: Api-Key <ключ>`; `folderId` обязателен
в теле каждого запроса.
"""

from __future__ import annotations

import json
import time
import urllib.parse

import requests

# Символы, которые GetDynamics не принимает: поддерживается только оператор `+`.
FORBIDDEN_PHRASE_CHARS = '!"«»[]()|'

# Коды, по которым имеет смысл повторить вызов.
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# Вызовы, завершившиеся внутренней ошибкой сервера или ошибкой авторизации,
# не тарифицируются (ТЗ, п. 8). 429 — отказ до обработки, тоже не считаем.
NON_BILLABLE_STATUSES = {401, 403, 429, 500, 502, 503, 504}


class WordstatError(RuntimeError):
    """Ошибка обращения к Wordstat API."""


class WordstatHTTPError(WordstatError):
    def __init__(self, status: int, body: str, path: str):
        self.status = status
        self.body = body
        self.path = path
        super().__init__(f"{path}: HTTP {status}: {body[:500]}")


def validate_phrase(phrase: str) -> None:
    """Проверить фразу до вызова: сервер отвергает операторы кроме `+`."""
    if not phrase or not phrase.strip():
        raise ValueError("пустая фраза")
    if len(phrase) > 400:
        raise ValueError(f"фраза длиннее 400 символов: {phrase[:60]}…")
    bad = sorted({c for c in phrase if c in FORBIDDEN_PHRASE_CHARS})
    if bad:
        raise ValueError(
            f"фраза {phrase!r} содержит операторы {''.join(bad)}, "
            "GetDynamics принимает только `+`"
        )


class WordstatClient:
    """Тонкая обёртка: пауза между вызовами, ретраи, учёт стоимости."""

    def __init__(
        self,
        api_key: str,
        folder_id: str,
        base_url: str = "https://searchapi.api.cloud.yandex.net/v2/wordstat",
        pause_seconds: float = 0.3,
        max_attempts: int = 5,
        backoff_base_seconds: float = 2.0,
        backoff_cap_seconds: float = 60.0,
        timeout_seconds: float = 30.0,
        price_per_call_rub: float = 0.02,
        session: requests.Session | None = None,
        sleep=time.sleep,
    ):
        if not api_key:
            raise WordstatError("не задан YANDEX_API_KEY")
        if not folder_id:
            raise WordstatError("не задан YANDEX_FOLDER_ID")
        self.api_key = api_key
        self.folder_id = folder_id
        self.base_url = base_url.rstrip("/")
        self.pause_seconds = pause_seconds
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_cap_seconds = backoff_cap_seconds
        self.timeout_seconds = timeout_seconds
        self.price_per_call_rub = price_per_call_rub
        self.session = session or requests.Session()
        self._sleep = sleep
        self._last_call_at = 0.0
        self.stats = {
            "http_calls": 0,        # всего обращений, включая повторные
            "billable_calls": 0,    # из них тарифицируемых
            "retries": 0,
            "failures": 0,          # вызовов, не давших результата после всех попыток
        }

    # -- стоимость ---------------------------------------------------------

    @property
    def cost_rub(self) -> float:
        return round(self.stats["billable_calls"] * self.price_per_call_rub, 4)

    # -- транспорт ---------------------------------------------------------

    def _throttle(self) -> None:
        waited = time.monotonic() - self._last_call_at
        if self._last_call_at and waited < self.pause_seconds:
            self._sleep(self.pause_seconds - waited)

    def _post(self, path: str, payload: dict) -> dict:
        url = urllib.parse.urljoin(self.base_url + "/", path.lstrip("/"))
        body = dict(payload)
        body["folderId"] = self.folder_id
        headers = {
            "Authorization": f"Api-Key {self.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            self.stats["http_calls"] += 1
            try:
                response = self.session.post(
                    url,
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:      # сеть: повторяем
                last_error = exc
                self._last_call_at = time.monotonic()
                if attempt == self.max_attempts:
                    break
                self.stats["retries"] += 1
                self._sleep(self._backoff(attempt))
                continue

            self._last_call_at = time.monotonic()
            status = response.status_code
            if status not in NON_BILLABLE_STATUSES:
                self.stats["billable_calls"] += 1

            if status == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise WordstatError(f"{path}: ответ не JSON: {response.text[:300]}") from exc

            error = WordstatHTTPError(status, response.text, path)
            if status not in RETRYABLE_STATUSES or attempt == self.max_attempts:
                if status in RETRYABLE_STATUSES:
                    last_error = error
                    break
                self.stats["failures"] += 1
                raise error

            last_error = error
            self.stats["retries"] += 1
            self._sleep(self._retry_after(response) or self._backoff(attempt))

        self.stats["failures"] += 1
        raise WordstatError(
            f"{path}: не удалось получить ответ за {self.max_attempts} попыток: {last_error}"
        ) from last_error

    def _backoff(self, attempt: int) -> float:
        return min(self.backoff_base_seconds * (2 ** (attempt - 1)), self.backoff_cap_seconds)

    @staticmethod
    def _retry_after(response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None

    # -- методы API --------------------------------------------------------

    def get_dynamics(
        self,
        phrase: str,
        from_date: str,
        to_date: str,
        period: str,
        regions: list[str] | None = None,
        devices: list[str] | None = None,
    ) -> dict:
        """GetDynamics: частотность фразы по периодам.

        `to_date` обязан быть последним днём периода — выравнивание делает
        вызывающий код (`ws.periods.period_end`).
        """
        validate_phrase(phrase)
        payload: dict = {
            "phrase": phrase,
            "period": period,
            "fromDate": from_date,
            "toDate": to_date,
        }
        if regions:
            payload["regions"] = [str(r) for r in regions]
        if devices:
            payload["devices"] = list(devices)
        return self._post("dynamics", payload)

    def get_regions_tree(self) -> dict:
        """GetRegionsTree: справочник регионов. Не тарифицируется."""
        return self._post("getRegionsTree", {})

    def get_top(
        self,
        phrase: str,
        regions: list[str] | None = None,
        devices: list[str] | None = None,
        num_phrases: int | None = None,
    ) -> dict:
        """GetTop: топ вложенных запросов за 30 дней — для разбора омонимов."""
        payload: dict = {"phrase": phrase}
        if regions:
            payload["regions"] = [str(r) for r in regions]
        if devices:
            payload["devices"] = list(devices)
        if num_phrases:
            payload["numPhrases"] = int(num_phrases)
        return self._post("topRequests", payload)


def dynamics_rows(response: dict) -> list[dict]:
    """Ряд из ответа GetDynamics: `count` приходит строкой, приводим к int."""
    rows = []
    for item in response.get("results") or response.get("dynamics") or []:
        share = item.get("share")
        rows.append(
            {
                "date": item["date"],
                "count": int(item["count"]),
                "share": float(share) if share is not None else None,
            }
        )
    return rows
