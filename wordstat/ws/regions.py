"""Справочник регионов: выгрузка дерева и сопоставление с нашим периметром.

Периметр кампании задан городами, Вордстат считает по регионам. Решение,
зафиксированное в `regions.yaml` (`meta.level`), одно для кампании и для
контроля — иначе сравнение групп некорректно.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import yaml

HEADER = """\
# Справочник регионов: наше название -> ID Вордстата -> тип (кампания / контроль).
#
# Файл собирается командой `python3 run.py regions resolve` из ответа
# GetRegionsTree. Руками правится только периметр (name / match / type /
# cities / screens / campaign_start); поле region_id заполняет скрипт.
#
# Уровень (город или область) — общий для обеих групп, см. meta.level.
"""


def walk(node, path=()):
    """Обойти дерево регионов, отдавая (узел, путь предков)."""
    if isinstance(node, dict) and ("regions" in node or "root" in node) and "id" not in node:
        children = node.get("regions") or node.get("root") or []
        if isinstance(children, dict):
            children = [children]
        for child in children:
            yield from walk(child, path)
        return
    if isinstance(node, list):
        for child in node:
            yield from walk(child, path)
        return
    if not isinstance(node, dict):
        return
    yield node, path
    for child in node.get("children") or []:
        yield from walk(child, path + (node,))


def normalize(name: str) -> str:
    """Нормализация названия для сопоставления: регистр, ё, пробелы, точки."""
    text = str(name).lower().replace("ё", "е").replace("-", " ").replace(".", " ")
    return " ".join(text.split())


def node_label(node: dict) -> str:
    for key in ("label", "name", "title"):
        if node.get(key):
            return str(node[key])
    return ""


def node_id(node: dict):
    for key in ("id", "regionId", "region_id"):
        if node.get(key) is not None:
            return str(node[key])
    return None


def index_tree(tree) -> dict[str, list[dict]]:
    """Индекс `нормализованное название -> [узлы]`."""
    index: dict[str, list[dict]] = {}
    for node, path in walk(tree):
        label = node_label(node)
        rid = node_id(node)
        if not label or rid is None:
            continue
        index.setdefault(normalize(label), []).append(
            {
                "id": rid,
                "label": label,
                "parents": [node_label(p) for p in path],
            }
        )
    return index


def resolve_one(index: dict[str, list[dict]], names) -> dict:
    """Найти регион по списку названий-кандидатов.

    Возвращает `{"id", "label", "candidates"}`. Неоднозначность и промах —
    не ошибка исполнения, а материал для отчёта: решает заказчик.
    """
    for name in names:
        hits = index.get(normalize(name)) or []
        if len(hits) == 1:
            return {"id": hits[0]["id"], "label": hits[0]["label"], "candidates": []}
        if len(hits) > 1:
            return {"id": None, "label": None, "candidates": hits}
    # мягкий поиск: название как подстрока
    soft: list[dict] = []
    for name in names:
        needle = normalize(name)
        for key, hits in index.items():
            if needle and needle in key:
                soft.extend(hits)
    uniq = {h["id"]: h for h in soft}
    if len(uniq) == 1:
        only = next(iter(uniq.values()))
        return {"id": only["id"], "label": only["label"], "candidates": []}
    return {"id": None, "label": None, "candidates": list(uniq.values())[:10]}


def dump_regions(meta: dict, regions: list[dict]) -> str:
    """Собрать текст `regions.yaml` — с шапкой-комментарием и стабильным порядком."""
    payload = {"meta": meta, "regions": regions}
    body = yaml.safe_dump(
        payload, allow_unicode=True, sort_keys=False, default_flow_style=False, width=100
    )
    return HEADER + "\n" + body


def region_to_dict(region, region_id=None, wordstat_label=None) -> dict:
    item = {
        "name": region.name,
        "type": region.type,
        "region_id": region_id if region_id is not None else region.region_id,
    }
    if wordstat_label:
        item["wordstat_label"] = wordstat_label
    if region.match:
        item["match"] = list(region.match)
    if region.cities:
        item["cities"] = list(region.cities)
    if region.screens:
        item["screens"] = region.screens
    if region.campaign_start:
        item["campaign_start"] = region.campaign_start.isoformat()
    if region.note:
        item["note"] = region.note
    return item


def save_tree(tree, path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_tree(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


def today_iso() -> str:
    return dt.date.today().isoformat()
