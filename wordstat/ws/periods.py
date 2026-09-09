"""Границы периодов Вордстата.

Единственное место, где живёт арифметика недель. Правило из ТЗ, п. 8:
сетку недель не пересобираем сами — берём дату из ответа API и только
достраиваем до неё границы. С какого дня Вордстат начинает неделю,
определяется по фактическим данным (`detect_week_anchor`), а не по догадке.
"""

from __future__ import annotations

import calendar
import datetime as dt

PERIOD_DAILY = "PERIOD_DAILY"
PERIOD_WEEKLY = "PERIOD_WEEKLY"
PERIOD_MONTHLY = "PERIOD_MONTHLY"
PERIODS = (PERIOD_DAILY, PERIOD_WEEKLY, PERIOD_MONTHLY)


def parse_date(value) -> dt.date:
    """Дата из строки `YYYY-MM-DD`, `date` или `datetime`."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        return dt.date.fromisoformat(value.strip()[:10])
    raise TypeError(f"не дата: {value!r}")


def parse_api_date(value: str) -> dt.date:
    """Дата из ответа API: `2026-01-31T00:00:00Z` или `2026-01-31`."""
    return dt.date.fromisoformat(str(value).strip()[:10])


def week_start(day: dt.date) -> dt.date:
    """Понедельник недели, в которую попал день."""
    return day - dt.timedelta(days=day.weekday())


def week_end(day: dt.date) -> dt.date:
    """Воскресенье недели, в которую попал день."""
    return week_start(day) + dt.timedelta(days=6)


def month_end(day: dt.date) -> dt.date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def period_end(day: dt.date, period: str) -> dt.date:
    """Последний день периода, в который попал `day`.

    API строго требует, чтобы `toDate` был последним днём периода:
    иначе `InvalidArgument: The to field value should be the last day of ...`.
    """
    if period == PERIOD_DAILY:
        return day
    if period == PERIOD_WEEKLY:
        return week_end(day)
    if period == PERIOD_MONTHLY:
        return month_end(day)
    raise ValueError(f"неизвестный период: {period}")


def rfc3339_from(day: dt.date) -> str:
    return f"{day.isoformat()}T00:00:00Z"


def rfc3339_to(day: dt.date) -> str:
    return f"{day.isoformat()}T23:59:59Z"


def detect_week_anchor(dates) -> str:
    """С какого края недели API проставляет дату: `start` или `end`.

    Все даты понедельники -> `start`, все воскресенья -> `end`.
    Смешанный набор -> ошибка: это значит, что предположение о недельной
    сетке неверно, и молча достраивать границы нельзя.
    """
    weekdays = {parse_api_date(d).weekday() for d in dates}
    if not weekdays:
        raise ValueError("нет дат, по которым определять начало недели")
    if weekdays == {0}:
        return "start"
    if weekdays == {6}:
        return "end"
    raise ValueError(
        "даты в ответе API не ложатся на одну границу недели: "
        f"дни недели {sorted(weekdays)} (0 = понедельник). "
        "Сетка недель не такая, как предполагает скрипт, — разбираться руками"
    )


def week_bounds(day: dt.date, anchor: str) -> tuple[dt.date, dt.date]:
    """Начало и конец недели по дате из ответа API."""
    if anchor == "start":
        start = day
    elif anchor == "end":
        start = day - dt.timedelta(days=6)
    else:
        raise ValueError(f"неизвестный якорь недели: {anchor}")
    return start, start + dt.timedelta(days=6)


def period_bounds(day: dt.date, period: str, anchor: str) -> tuple[dt.date, dt.date]:
    """Границы периода по дате из ответа API."""
    if period == PERIOD_DAILY:
        return day, day
    if period == PERIOD_WEEKLY:
        return week_bounds(day, anchor)
    if period == PERIOD_MONTHLY:
        if anchor == "start":
            return day, month_end(day)
        return day.replace(day=1), day
    raise ValueError(f"неизвестный период: {period}")


def is_closed(period_last_day: dt.date, today: dt.date | None = None) -> bool:
    """Период закрыт, если его последний день уже прошёл."""
    today = today or dt.date.today()
    return period_last_day < today
