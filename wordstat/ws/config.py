"""Чтение конфигурации: `config.yaml`, `regions.yaml`, `.env`.

Периметр (фразы, регионы, даты) живёт в yaml. Добавление города или фразы
не требует правки кода — это требование ТЗ, п. 5.2.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import pathlib

import yaml

from . import periods

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"
ENV_FILE = ROOT / ".env"

REGION_TYPES = ("campaign", "control")


def load_env(path: pathlib.Path = ENV_FILE) -> None:
    """Подтянуть `.env`, не затирая уже выставленные переменные окружения."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def credentials() -> tuple[str, str]:
    """Ключ и каталог — только из окружения, в репозитории их нет."""
    load_env()
    key = os.environ.get("YANDEX_API_KEY", "")
    folder = os.environ.get("YANDEX_FOLDER_ID", "")
    missing = [n for n, v in (("YANDEX_API_KEY", key), ("YANDEX_FOLDER_ID", folder)) if not v]
    if missing:
        raise SystemExit(
            "Нет доступов: " + ", ".join(missing) + ".\n"
            "Скопируйте .env.example в .env и заполните значениями от заказчика "
            "(см. README, раздел «Доступы»)."
        )
    return key, folder


@dataclasses.dataclass(frozen=True)
class Region:
    name: str
    type: str
    region_id: str | None
    match: tuple[str, ...] = ()
    cities: tuple[str, ...] = ()
    screens: int = 0
    campaign_start: dt.date | None = None
    note: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.region_id)


@dataclasses.dataclass(frozen=True)
class Config:
    path: pathlib.Path
    base_url: str
    pause_seconds: float
    max_attempts: int
    backoff_base_seconds: float
    timeout_seconds: float
    price_per_call_rub: float
    start_date: dt.date
    end_date: dt.date | None
    period: str
    week_anchor: str
    include_current_period: bool
    devices: tuple[str, ...]
    phrases: tuple[str, ...]
    regions_file: pathlib.Path
    raw_dir: pathlib.Path
    output_dir: pathlib.Path
    regions_meta: dict
    regions: tuple[Region, ...]

    # -- удобные выборки ---------------------------------------------------

    @property
    def campaign_regions(self) -> tuple[Region, ...]:
        return tuple(r for r in self.regions if r.type == "campaign")

    @property
    def control_regions(self) -> tuple[Region, ...]:
        return tuple(r for r in self.regions if r.type == "control")

    @property
    def unresolved_regions(self) -> tuple[Region, ...]:
        return tuple(r for r in self.regions if not r.resolved)

    def resolved_regions(self) -> tuple[Region, ...]:
        return tuple(r for r in self.regions if r.resolved)

    def effective_end_date(self, today: dt.date | None = None) -> dt.date:
        """Конец ряда: из конфига или последний закрытый период.

        Верхняя граница выравнивается на конец периода — иначе API отвечает
        `The to field value should be the last day of the ...`. Незакрытый
        текущий период по умолчанию не запрашивается: в него попадает
        неполная неделя, а даты в будущем API может не принять. Включается
        флагом `series.include_current_period` — тогда неполная неделя
        помечается в CSV колонкой `is_partial`.
        """
        today = today or dt.date.today()
        end = periods.period_end(self.end_date or today, self.period)
        if self.include_current_period:
            return end
        while end >= today:
            first_day = end - dt.timedelta(days=6) if self.period == periods.PERIOD_WEEKLY else end.replace(day=1)
            if self.period == periods.PERIOD_DAILY:
                first_day = end
            end = periods.period_end(first_day - dt.timedelta(days=1), self.period)
        return end

    def planned_calls(self) -> int:
        return len(self.phrases) * len(self.resolved_regions()) * len(self.devices)


def _as_date(value) -> dt.date | None:
    return None if value in (None, "") else periods.parse_date(value)


def load_regions(path: pathlib.Path) -> tuple[dict, tuple[Region, ...]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    meta = data.get("meta") or {}
    regions = []
    seen = set()
    for item in data.get("regions") or []:
        name = str(item["name"]).strip()
        if name in seen:
            raise SystemExit(f"{path}: регион {name!r} задан дважды")
        seen.add(name)
        rtype = str(item.get("type", "")).strip()
        if rtype not in REGION_TYPES:
            raise SystemExit(
                f"{path}: у региона {name!r} тип {rtype!r}, допустимы {REGION_TYPES}"
            )
        region_id = item.get("region_id")
        regions.append(
            Region(
                name=name,
                type=rtype,
                region_id=None if region_id in (None, "", "null") else str(region_id),
                match=tuple(item.get("match") or ()),
                cities=tuple(item.get("cities") or ()),
                screens=int(item.get("screens") or 0),
                campaign_start=_as_date(item.get("campaign_start")),
                note=str(item.get("note") or ""),
            )
        )
    if not regions:
        raise SystemExit(f"{path}: список регионов пуст")
    return meta, tuple(regions)


def load(path: pathlib.Path | str = DEFAULT_CONFIG) -> Config:
    path = pathlib.Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    api = data.get("api") or {}
    series = data.get("series") or {}
    paths = data.get("paths") or {}
    base = path.parent

    period = str(series.get("period", periods.PERIOD_WEEKLY))
    if period not in periods.PERIODS:
        raise SystemExit(f"{path}: период {period!r}, допустимы {periods.PERIODS}")
    anchor = str(series.get("week_anchor", "auto"))
    if anchor not in ("auto", "start", "end"):
        raise SystemExit(f"{path}: week_anchor {anchor!r}, допустимы auto/start/end")

    phrases = tuple(str(p).strip() for p in (data.get("phrases") or ()) if str(p).strip())
    if not phrases:
        raise SystemExit(f"{path}: список фраз пуст")
    devices = tuple(str(d) for d in (series.get("devices") or ("DEVICE_ALL",)))

    regions_file = base / str(data.get("regions_file", "regions.yaml"))
    regions_meta, regions = load_regions(regions_file)

    return Config(
        path=path,
        base_url=str(api.get("base_url", "https://searchapi.api.cloud.yandex.net/v2/wordstat")),
        pause_seconds=float(api.get("pause_seconds", 0.3)),
        max_attempts=int(api.get("max_attempts", 5)),
        backoff_base_seconds=float(api.get("backoff_base_seconds", 2)),
        timeout_seconds=float(api.get("timeout_seconds", 30)),
        price_per_call_rub=float(api.get("price_per_call_rub", 0.02)),
        start_date=periods.parse_date(series["start_date"]),
        end_date=_as_date(series.get("end_date")),
        period=period,
        week_anchor=anchor,
        include_current_period=bool(series.get("include_current_period", False)),
        devices=devices,
        phrases=phrases,
        regions_file=regions_file,
        raw_dir=base / str(paths.get("raw", "raw")),
        output_dir=base / str(paths.get("output", "output")),
        regions_meta=regions_meta,
        regions=regions,
    )
