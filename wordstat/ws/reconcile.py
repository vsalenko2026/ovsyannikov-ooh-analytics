"""Сверка выгрузки API с ручными выгрузками из веб-интерфейса Вордстата.

Обязательный этап приёмки (ТЗ, п. 6): пока сверка не пройдена, скрипт не
считается готовым. Расхождения не подгоняются коэффициентами — они
оформляются таблицей и выносятся на решение заказчика.
"""

from __future__ import annotations

import csv
import pathlib
import statistics

from . import periods
from .regions import normalize

# Приемлемое расхождение на отдельной неделе (ТЗ, п. 6).
WEEK_TOLERANCE_PCT = 2.0
# Признаки систематического расхождения — повод остановиться.
MEDIAN_STOP_PCT = 1.0
OUTLIER_STOP_PCT = 10.0

COLUMN_ALIASES = {
    "phrase": ("phrase", "фраза", "запрос", "ключ", "ключевая фраза", "keyword"),
    "region": ("region", "region_name", "регион", "область", "регион вордстата"),
    "date": ("date", "дата", "неделя", "период", "week", "week_start"),
    "count": ("count", "частота", "частотность", "количество", "запросов", "показов", "значение"),
}


def _match_columns(header: list[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    normalized = [normalize(h) for h in header]
    for field, aliases in COLUMN_ALIASES.items():
        for index, name in enumerate(normalized):
            if name in {normalize(a) for a in aliases}:
                found[field] = index
                break
    missing = [f for f in ("phrase", "region", "date", "count") if f not in found]
    if missing:
        raise SystemExit(
            "в ручной выгрузке не найдены колонки: " + ", ".join(missing) + ".\n"
            "Ожидается таблица с колонками фраза / регион / дата / частота "
            "(см. README, раздел «Сверка»). Заголовки: " + ", ".join(header)
        )
    return found


def _rows_from_table(table: list[list]) -> list[dict]:
    if not table:
        raise SystemExit("ручная выгрузка пуста")
    columns = _match_columns([str(c or "") for c in table[0]])
    rows = []
    for line in table[1:]:
        if not line or all(c in (None, "") for c in line):
            continue
        try:
            value = line[columns["count"]]
            count = int(float(str(value).replace(" ", "").replace("\xa0", "").replace(",", ".")))
            rows.append(
                {
                    "phrase": str(line[columns["phrase"]]).strip(),
                    "region": str(line[columns["region"]]).strip(),
                    "date": periods.parse_date(str(line[columns["date"]]).strip()[:10]),
                    "count": count,
                }
            )
        except (ValueError, IndexError, TypeError) as exc:
            raise SystemExit(f"не разобрана строка ручной выгрузки {line!r}: {exc}") from exc
    if not rows:
        raise SystemExit("в ручной выгрузке нет строк с данными")
    return rows


def load_manual(path: pathlib.Path) -> list[dict]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        sheet = load_workbook(path, data_only=True).active
        table = [list(r) for r in sheet.iter_rows(values_only=True)]
        return _rows_from_table(table)
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise SystemExit(f"ручная выгрузка {path} пуста")
    delimiter = "\t" if "\t" in text.splitlines()[0] else (";" if ";" in text.splitlines()[0] else ",")
    return _rows_from_table([row for row in csv.reader(text.splitlines(), delimiter=delimiter)])


def load_api(path: pathlib.Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [
            {
                "phrase": row["phrase"].strip(),
                "region": row["region_name"].strip(),
                "week_start": periods.parse_date(row["week_start"]),
                "week_end": periods.parse_date(row["week_end"]),
                "count": int(row["count"]),
                "is_partial": row.get("is_partial") == "1",
                "device": row.get("device", ""),
            }
            for row in csv.DictReader(handle)
        ]


def region_aliases(cfg) -> dict[str, str]:
    """Названия, по которым в ручной выгрузке может быть назван регион."""
    aliases: dict[str, str] = {}
    for region in cfg.regions:
        for name in (region.name, *region.match, *region.cities):
            aliases[normalize(name)] = region.name
    return aliases


def compare(cfg, manual: list[dict], api: list[dict], device: str = "DEVICE_ALL") -> list[dict]:
    aliases = region_aliases(cfg)
    api_index: dict[tuple, dict] = {}
    for row in api:
        if row["device"] and row["device"] != device:
            continue
        api_index[(normalize(row["phrase"]), normalize(row["region"]), row["week_start"])] = row

    result = []
    for row in manual:
        region = aliases.get(normalize(row["region"]), row["region"])
        week = periods.week_start(row["date"])
        found = api_index.get((normalize(row["phrase"]), normalize(region), week))
        deviation = None
        if found and row["count"]:
            deviation = (found["count"] - row["count"]) / row["count"] * 100
        elif found and not row["count"]:
            deviation = 0.0 if found["count"] == 0 else float("inf")
        result.append(
            {
                "phrase": row["phrase"],
                "region": region,
                "week_start": week,
                "manual": row["count"],
                "api": found["count"] if found else None,
                "deviation_pct": deviation,
                "partial": bool(found and found["is_partial"]),
            }
        )
    result.sort(key=lambda r: (r["phrase"], r["region"], r["week_start"]))
    return result


def verdict(rows: list[dict]) -> dict:
    matched = [r for r in rows if r["api"] is not None and r["deviation_pct"] is not None]
    missing = [r for r in rows if r["api"] is None]
    deviations = [abs(r["deviation_pct"]) for r in matched if r["deviation_pct"] != float("inf")]
    manual_total = sum(r["manual"] for r in matched)
    api_total = sum(r["api"] for r in matched)
    ratio = (api_total / manual_total) if manual_total else None

    stop_reasons = []
    if missing:
        stop_reasons.append(f"{len(missing)} строк ручной выгрузки не нашли пары в API")
    if deviations:
        median = statistics.median(deviations)
        worst = max(deviations)
        if median > MEDIAN_STOP_PCT:
            stop_reasons.append(f"медианное расхождение {median:.1f}% > {MEDIAN_STOP_PCT}%")
        if worst > OUTLIER_STOP_PCT:
            stop_reasons.append(f"максимальное расхождение {worst:.1f}% > {OUTLIER_STOP_PCT}%")
    else:
        median = worst = None
        stop_reasons.append("не с чем сравнивать: ни одной сопоставленной недели")
    if ratio is not None and abs(ratio - 1) > 0.02:
        stop_reasons.append(f"суммарное отношение API/ручная = {ratio:.3f}")

    return {
        "matched": len(matched),
        "missing": len(missing),
        "median_pct": median,
        "worst_pct": worst,
        "ratio": ratio,
        "ok": not stop_reasons,
        "stop_reasons": stop_reasons,
    }


def _fmt(value, digits=1, suffix=""):
    return "—" if value is None else f"{value:.{digits}f}{suffix}"


def render_markdown(rows: list[dict], summary: dict) -> str:
    lines = [
        "# Сверка выгрузки API с ручной выгрузкой Вордстата",
        "",
        f"Сопоставлено недель: {summary['matched']}; без пары: {summary['missing']}.",
        f"Медианное расхождение: {_fmt(summary['median_pct'], 2, '%')}; "
        f"максимальное: {_fmt(summary['worst_pct'], 2, '%')}; "
        f"суммарное отношение API/ручная: {_fmt(summary['ratio'], 3)}.",
        "",
    ]
    if summary["ok"]:
        lines += [
            f"**Итог: сверка пройдена.** Расхождения в пределах {WEEK_TOLERANCE_PCT:.0f}% "
            "на отдельных неделях, систематического сдвига нет.",
            "",
        ]
    else:
        lines += [
            "**Итог: СТОП.** " + "; ".join(summary["stop_reasons"]) + ".",
            "",
            "Коэффициентами не подгонять. Проверить в таком порядке: операторы в фразе, "
            "трактовку словоформ, ID региона в справочнике, границы недель.",
            "",
        ]
    lines += [
        "| Фраза | Регион | Неделя | Ручная | API | Расхождение |",
        "|---|---|---|---:|---:|---:|",
    ]
    for row in rows:
        api = "—" if row["api"] is None else str(row["api"])
        dev = "—" if row["deviation_pct"] is None else f"{row['deviation_pct']:+.1f}%"
        mark = " ⚠" if row["deviation_pct"] is not None and abs(row["deviation_pct"]) > WEEK_TOLERANCE_PCT else ""
        partial = " (неделя не закрыта)" if row["partial"] else ""
        lines.append(
            f"| {row['phrase']} | {row['region']} | {row['week_start']:%d.%m.%Y} | "
            f"{row['manual']} | {api} | {dev}{mark}{partial} |"
        )
    return "\n".join(lines) + "\n"


def write_csv(rows: list[dict], path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["phrase", "region_name", "week_start", "manual_count", "api_count", "deviation_pct"])
        for row in rows:
            writer.writerow([
                row["phrase"], row["region"], row["week_start"].isoformat(),
                row["manual"], "" if row["api"] is None else row["api"],
                "" if row["deviation_pct"] is None else f"{row['deviation_pct']:.4f}",
            ])
    return path
