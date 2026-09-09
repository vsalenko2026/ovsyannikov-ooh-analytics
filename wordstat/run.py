#!/usr/bin/env python3
"""Выгрузка брендового спроса из Вордстата через Yandex Search API.

    python3 run.py plan                 план вызовов и стоимость, без сети
    python3 run.py regions fetch-tree   выкачать справочник регионов
    python3 run.py regions resolve      проставить region_id в regions.yaml
    python3 run.py probe-regions        проверить, суммирует ли API несколько регионов
    python3 run.py fetch                выгрузить динамику (кэш, ретраи, лог стоимости)
    python3 run.py build                собрать wordstat_long.csv и wordstat_wide.xlsx
    python3 run.py reconcile --manual … сверка с ручной выгрузкой
    python3 run.py top --phrase …       топ вложенных запросов (разбор омонимов)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from ws import build as build_mod
from ws import config as config_mod
from ws import fetch as fetch_mod
from ws import periods
from ws import reconcile as reconcile_mod
from ws import regions as regions_mod
from ws.api import WordstatClient, dynamics_rows

TREE_PATH = config_mod.ROOT / "regions_tree.json"


def make_client(cfg) -> WordstatClient:
    key, folder = config_mod.credentials()
    return WordstatClient(
        api_key=key,
        folder_id=folder,
        base_url=cfg.base_url,
        pause_seconds=cfg.pause_seconds,
        max_attempts=cfg.max_attempts,
        backoff_base_seconds=cfg.backoff_base_seconds,
        timeout_seconds=cfg.timeout_seconds,
        price_per_call_rub=cfg.price_per_call_rub,
    )


def require_resolved(cfg) -> None:
    if cfg.unresolved_regions:
        names = ", ".join(r.name for r in cfg.unresolved_regions)
        raise SystemExit(
            f"в {cfg.regions_file.name} не проставлены region_id: {names}.\n"
            "Сначала: python3 run.py regions fetch-tree && python3 run.py regions resolve"
        )


# --- команды ---------------------------------------------------------------

def cmd_plan(cfg, args) -> int:
    today = periods.parse_date(args.today) if args.today else dt.date.today()
    resolved = cfg.resolved_regions()
    print(f"конфигурация      : {cfg.path}")
    print(f"фраз              : {len(cfg.phrases)} — {', '.join(cfg.phrases)}")
    print(f"регионов           : {len(cfg.regions)} "
          f"(кампания {len(cfg.campaign_regions)}, контроль {len(cfg.control_regions)})")
    print(f"из них с region_id : {len(resolved)}")
    if cfg.unresolved_regions:
        print("  без ID           : " + ", ".join(r.name for r in cfg.unresolved_regions))
    print(f"устройств          : {len(cfg.devices)} — {', '.join(cfg.devices)}")
    start = periods.week_start(cfg.start_date) if cfg.period == periods.PERIOD_WEEKLY else cfg.start_date
    print(f"период             : {cfg.period}, {start} … {cfg.effective_end_date(today)}")
    calls = cfg.planned_calls()
    print(f"вызовов за прогон  : {calls}")
    print(f"стоимость прогона  : {calls * cfg.price_per_call_rub:.2f} ₽ "
          f"(по {cfg.price_per_call_rub} ₽ за вызов)")
    return 0


def cmd_regions_fetch_tree(cfg, args) -> int:
    client = make_client(cfg)
    tree = client.get_regions_tree()
    regions_mod.save_tree(tree, TREE_PATH)
    stamp = regions_mod.today_iso()
    regions_mod.save_tree(tree, cfg.raw_dir / "regions_tree" / f"{stamp}.json")
    total = sum(1 for _ in regions_mod.walk(tree))
    print(f"справочник сохранён: {TREE_PATH} ({total} узлов, вызов не тарифицируется)")
    return 0


def cmd_regions_resolve(cfg, args) -> int:
    tree_path = pathlib.Path(args.tree) if args.tree else TREE_PATH
    if not tree_path.exists():
        raise SystemExit(f"нет {tree_path} — сначала `python3 run.py regions fetch-tree`")
    index = regions_mod.index_tree(regions_mod.load_tree(tree_path))

    items, unresolved = [], []
    for region in cfg.regions:
        names = [region.name, *region.match]
        hit = regions_mod.resolve_one(index, names)
        if hit["id"]:
            print(f"  {region.name:32} -> {hit['id']} ({hit['label']})")
            items.append(regions_mod.region_to_dict(region, hit["id"], hit["label"]))
        else:
            unresolved.append((region, hit["candidates"]))
            print(f"  {region.name:32} -> НЕ НАЙДЕН")
            for candidate in hit["candidates"]:
                parents = " / ".join(candidate["parents"][-2:])
                print(f"      кандидат: {candidate['id']} {candidate['label']} [{parents}]")
            items.append(regions_mod.region_to_dict(region, None, None))

    meta = dict(cfg.regions_meta)
    meta["resolved_at"] = regions_mod.today_iso()
    meta["regions_tree_fetched_at"] = dt.date.fromtimestamp(tree_path.stat().st_mtime).isoformat()
    cfg.regions_file.write_text(regions_mod.dump_regions(meta, items), encoding="utf-8")
    print(f"\n{cfg.regions_file} обновлён: {len(items) - len(unresolved)} из {len(items)} с ID")
    if unresolved:
        print("Не разрешились — выбрать узел руками и вписать region_id: "
              + ", ".join(r.name for r, _ in unresolved))
        return 1
    return 0


def cmd_probe_regions(cfg, args) -> int:
    """Эмпирическая проверка: несколько регионов в одном вызове — сумма или разбивка?"""
    require_resolved(cfg)
    first = cfg.campaign_regions[0]
    second = cfg.control_regions[0]
    phrase = cfg.phrases[0]
    to_date = cfg.effective_end_date()
    from_date = periods.week_start(to_date - dt.timedelta(weeks=8))
    client = make_client(cfg)

    def call(region_ids):
        return client.get_dynamics(
            phrase=phrase,
            from_date=periods.rfc3339_from(from_date),
            to_date=periods.rfc3339_to(to_date),
            period=cfg.period,
            regions=region_ids,
            devices=[cfg.devices[0]],
        )

    a, b, both = call([first.region_id]), call([second.region_id]), call([first.region_id, second.region_id])
    out = cfg.raw_dir / "probe" / regions_mod.today_iso()
    out.mkdir(parents=True, exist_ok=True)
    for name, payload in (("a", a), ("b", b), ("a_plus_b", both)):
        (out / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    sum_a = sum(r["count"] for r in dynamics_rows(a))
    sum_b = sum(r["count"] for r in dynamics_rows(b))
    sum_both = sum(r["count"] for r in dynamics_rows(both))
    print(f"фраза: {phrase}, период {from_date} … {to_date}")
    print(f"  {first.name}: {sum_a}")
    print(f"  {second.name}: {sum_b}")
    print(f"  оба региона одним вызовом: {sum_both} (сумма по отдельности: {sum_a + sum_b})")
    if sum_a + sum_b and abs(sum_both - (sum_a + sum_b)) <= 0.02 * (sum_a + sum_b):
        print("Вывод: API возвращает один суммарный ряд. Выгружаем по одному региону на вызов.")
    else:
        print("Вывод: суммой не объясняется — разобрать структуру ответа руками, "
              "сырые файлы в " + str(out))
    print(f"стоимость проверки: {client.cost_rub:.2f} ₽")
    return 0


def cmd_fetch(cfg, args) -> int:
    require_resolved(cfg)
    run_date = periods.parse_date(args.run_date) if args.run_date else dt.date.today()
    plan = fetch_mod.build_plan(cfg, run_date)
    if args.dry_run:
        for call in plan:
            print(f"  {call.describe()} · {call.from_date} … {call.to_date}")
        print(f"вызовов: {len(plan)}; оценка стоимости: "
              f"{len(plan) * cfg.price_per_call_rub:.2f} ₽")
        return 0

    client = make_client(cfg)
    print(f"прогон {run_date}: {len(plan)} вызовов, каталог {fetch_mod.run_dir(cfg, run_date)}")
    summary = fetch_mod.fetch(cfg, client, run_date=run_date)
    print("\nитог прогона")
    print(f"  запланировано вызовов : {summary['planned']}")
    print(f"  выгружено             : {summary['fetched']}")
    print(f"  взято из кэша         : {summary['from_cache']}")
    print(f"  ошибок                : {len(summary['errors'])}")
    print(f"  обращений к API       : {summary['http_calls']} (повторов: {summary['retries']})")
    print(f"  тарифицируемых        : {summary['billable_calls']}")
    print(f"  стоимость прогона     : {summary['cost_rub']:.2f} ₽")
    if summary["errors"]:
        print("\nне выгружено:")
        for error in summary["errors"]:
            print(f"  {error['call']}: {error['error']}")
        print("Повторный запуск доберёт недостающее — уже полученное берётся из кэша.")
        return 1
    return 0


def cmd_build(cfg, args) -> int:
    raw_dir = pathlib.Path(args.raw) if args.raw else fetch_mod.latest_run(cfg)
    out_dir = pathlib.Path(args.out) if args.out else cfg.output_dir / raw_dir.name
    today = periods.parse_date(args.today) if args.today else None
    print(f"сырые ответы: {raw_dir}\nрезультат   : {out_dir}")
    summary = build_mod.build(cfg, raw_dir, out_dir, force=args.force, today=today)
    print(f"  {summary['long_csv']}")
    print(f"  {summary['wide_xlsx']}")
    return 0


def cmd_reconcile(cfg, args) -> int:
    manual_path = pathlib.Path(args.manual)
    if args.api:
        api_path = pathlib.Path(args.api)
    else:
        latest = sorted(p for p in cfg.output_dir.glob("*/wordstat_long.csv"))
        if not latest:
            raise SystemExit("не найден wordstat_long.csv — сначала `run.py build`")
        api_path = latest[-1]
    manual = reconcile_mod.load_manual(manual_path)
    api = reconcile_mod.load_api(api_path)
    rows = reconcile_mod.compare(cfg, manual, api, device=args.device)
    summary = reconcile_mod.verdict(rows)
    out_dir = pathlib.Path(args.out) if args.out else api_path.parent
    md_path = out_dir / "сверка_с_ручной_выгрузкой.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(reconcile_mod.render_markdown(rows, summary), encoding="utf-8")
    reconcile_mod.write_csv(rows, out_dir / "сверка_с_ручной_выгрузкой.csv")
    print(f"ручная выгрузка : {manual_path} ({len(manual)} строк)")
    print(f"выгрузка API    : {api_path}")
    print(f"сопоставлено    : {summary['matched']}, без пары: {summary['missing']}")
    if summary["median_pct"] is not None:
        print(f"расхождение     : медиана {summary['median_pct']:.2f}%, "
              f"максимум {summary['worst_pct']:.2f}%")
    print(f"таблица сверки  : {md_path}")
    if summary["ok"]:
        print("ИТОГ: сверка пройдена")
        return 0
    print("ИТОГ: СТОП — " + "; ".join(summary["stop_reasons"]))
    return 1


def cmd_top(cfg, args) -> int:
    require_resolved(cfg)
    region = next((r for r in cfg.regions if regions_mod.normalize(r.name) == regions_mod.normalize(args.region)), None)
    if region is None:
        raise SystemExit(f"регион {args.region!r} не найден в {cfg.regions_file.name}")
    client = make_client(cfg)
    response = client.get_top(args.phrase, regions=[region.region_id], num_phrases=args.limit)
    print(f"{args.phrase} · {region.name} · всего: {response.get('totalCount', '—')}")
    for item in response.get("results", []):
        print(f"  {item['count']:>10}  {item['phrase']}")
    print(f"стоимость: {client.cost_rub:.2f} ₽")
    return 0


COMMANDS = {
    "plan": cmd_plan,
    "regions.fetch-tree": cmd_regions_fetch_tree,
    "regions.resolve": cmd_regions_resolve,
    "probe-regions": cmd_probe_regions,
    "fetch": cmd_fetch,
    "build": cmd_build,
    "reconcile": cmd_reconcile,
    "top": cmd_top,
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", default=str(config_mod.DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="план вызовов и стоимость, без обращений к API")
    p_plan.add_argument("--today", help="считать эту дату сегодняшней (YYYY-MM-DD)")

    p_regions = sub.add_parser("regions", help="справочник регионов")
    regions_sub = p_regions.add_subparsers(dest="subcommand", required=True)
    regions_sub.add_parser("fetch-tree", help="выкачать дерево регионов (GetRegionsTree)")
    p_resolve = regions_sub.add_parser("resolve", help="проставить region_id в regions.yaml")
    p_resolve.add_argument("--tree", help="путь к сохранённому дереву")

    sub.add_parser("probe-regions", help="проверить, суммирует ли API несколько регионов")

    p_fetch = sub.add_parser("fetch", help="выгрузить динамику")
    p_fetch.add_argument("--run-date", help="дата прогона (YYYY-MM-DD), по умолчанию сегодня")
    p_fetch.add_argument("--dry-run", action="store_true", help="показать план, не вызывая API")

    p_build = sub.add_parser("build", help="собрать CSV и XLSX из сырых ответов")
    p_build.add_argument("--raw", help="каталог прогона, по умолчанию последний")
    p_build.add_argument("--out", help="каталог результата")
    p_build.add_argument("--today", help="считать эту дату сегодняшней (для флага незакрытой недели)")
    p_build.add_argument("--force", action="store_true", help="перезаписать результат прогона")

    p_rec = sub.add_parser("reconcile", help="сверка с ручной выгрузкой")
    p_rec.add_argument("--manual", required=True, help="файл ручной выгрузки (csv/tsv/xlsx)")
    p_rec.add_argument("--api", help="wordstat_long.csv, по умолчанию последний собранный")
    p_rec.add_argument("--out", help="каталог для таблицы сверки")
    p_rec.add_argument("--device", default="DEVICE_ALL")

    p_top = sub.add_parser("top", help="топ вложенных запросов (GetTop) — разбор омонимов")
    p_top.add_argument("--phrase", required=True)
    p_top.add_argument("--region", required=True)
    p_top.add_argument("--limit", type=int, default=30)

    args = parser.parse_args(argv)
    cfg = config_mod.load(args.config)
    key = args.command if args.command != "regions" else f"regions.{args.subcommand}"
    return COMMANDS[key](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
