"""Выгрузчик: цикл «фраза × регион × устройство», кэш, ретраи, лог стоимости.

Сырой ответ сохраняется как есть — вместе с параметрами запроса. Любая цифра
в отчёте должна поднимать за собой исходник (ТЗ, п. 2).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import re

from . import periods
from .api import WordstatError

SLUG_RE = re.compile(r"[^0-9A-Za-zА-Яа-яЁё]+")


def slug(text: str, limit: int = 40) -> str:
    return SLUG_RE.sub("-", str(text)).strip("-")[:limit] or "x"


@dataclasses.dataclass(frozen=True)
class Call:
    phrase: str
    region_name: str
    region_id: str
    region_type: str
    device: str
    period: str
    from_date: dt.date
    to_date: dt.date

    def request_body(self) -> dict:
        return {
            "phrase": self.phrase,
            "period": self.period,
            "fromDate": periods.rfc3339_from(self.from_date),
            "toDate": periods.rfc3339_to(self.to_date),
            "regions": [self.region_id],
            "devices": [self.device],
        }

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.request_body(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]

    @property
    def filename(self) -> str:
        return f"{self.region_id}_{slug(self.phrase)}_{slug(self.device)}_{self.fingerprint}.json"

    def describe(self) -> str:
        return f"{self.region_name} · {self.phrase} · {self.device}"


def build_plan(cfg, today: dt.date | None = None) -> list[Call]:
    """Перечень вызовов. По одному региону на вызов — см. README, «Один регион на вызов»."""
    today = today or dt.date.today()
    from_date = periods.week_start(cfg.start_date) if cfg.period == periods.PERIOD_WEEKLY else cfg.start_date
    to_date = cfg.effective_end_date(today)
    if to_date <= from_date:
        raise SystemExit(f"пустой период: {from_date} … {to_date}")
    plan = []
    for phrase in cfg.phrases:
        for region in cfg.resolved_regions():
            for device in cfg.devices:
                plan.append(
                    Call(
                        phrase=phrase,
                        region_name=region.name,
                        region_id=region.region_id,
                        region_type=region.type,
                        device=device,
                        period=cfg.period,
                        from_date=from_date,
                        to_date=to_date,
                    )
                )
    return plan


def run_dir(cfg, run_date: dt.date) -> pathlib.Path:
    return cfg.raw_dir / run_date.isoformat()


def cached(path: pathlib.Path, call: Call) -> bool:
    """Ответ за эту дату прогона и эти параметры уже лежит на диске."""
    if not path.exists():
        return False
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    return stored.get("request") == call.request_body() and "response" in stored


def save(path: pathlib.Path, call: Call, response: dict, attempts: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "region_name": call.region_name,
            "region_type": call.region_type,
            "attempts": attempts,
        },
        "request": call.request_body(),
        "response": response,
    }
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def fetch(cfg, client, run_date: dt.date | None = None, log=print) -> dict:
    """Прогон. Перезапускаемый: уже полученное из кэша не перевыгружается."""
    run_date = run_date or dt.date.today()
    plan = build_plan(cfg, run_date)
    directory = run_dir(cfg, run_date)
    directory.mkdir(parents=True, exist_ok=True)

    summary = {
        "run_date": run_date.isoformat(),
        "planned": len(plan),
        "fetched": 0,
        "from_cache": 0,
        "errors": [],
        "period": cfg.period,
        "from_date": plan[0].from_date.isoformat() if plan else None,
        "to_date": plan[0].to_date.isoformat() if plan else None,
        "phrases": list(cfg.phrases),
        "devices": list(cfg.devices),
        "regions": len(cfg.resolved_regions()),
    }

    for index, call in enumerate(plan, 1):
        path = directory / call.filename
        if cached(path, call):
            summary["from_cache"] += 1
            continue
        try:
            response = client.get_dynamics(
                phrase=call.phrase,
                from_date=periods.rfc3339_from(call.from_date),
                to_date=periods.rfc3339_to(call.to_date),
                period=call.period,
                regions=[call.region_id],
                devices=[call.device],
            )
        except (WordstatError, ValueError) as exc:
            summary["errors"].append({"call": call.describe(), "error": str(exc)})
            log(f"  [{index}/{len(plan)}] ОШИБКА  {call.describe()}: {exc}")
            continue
        save(path, call, response, attempts=1)
        summary["fetched"] += 1
        log(f"  [{index}/{len(plan)}] ок      {call.describe()}")

    summary["http_calls"] = client.stats["http_calls"]
    summary["billable_calls"] = client.stats["billable_calls"]
    summary["retries"] = client.stats["retries"]
    summary["cost_rub"] = client.cost_rub
    (directory / "manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def latest_run(cfg) -> pathlib.Path:
    """Последний по дате каталог сырых ответов."""
    if not cfg.raw_dir.exists():
        raise SystemExit(f"нет каталога {cfg.raw_dir} — сначала `run.py fetch`")
    runs = sorted(
        p for p in cfg.raw_dir.iterdir()
        if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)
    )
    if not runs:
        raise SystemExit(f"в {cfg.raw_dir} нет прогонов — сначала `run.py fetch`")
    return runs[-1]
