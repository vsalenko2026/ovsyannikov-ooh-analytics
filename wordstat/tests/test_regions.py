import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ws import regions as regions_mod

TREE = {
    "regions": [
        {
            "id": "225", "label": "Россия",
            "children": [
                {"id": "3", "label": "Центральный федеральный округ", "children": [
                    {"id": "10841", "label": "Ярославская область", "children": [
                        {"id": "16", "label": "Ярославль"},
                        {"id": "967", "label": "Рыбинск"},
                    ]},
                    {"id": "10645", "label": "Орловская область"},
                ]},
                {"id": "52", "label": "Уральский федеральный округ", "children": [
                    {"id": "11176", "label": "Тюменская область", "children": [
                        {"id": "55", "label": "Тюмень"},
                        {"id": "11162", "label": "Ханты-Мансийский АО"},
                    ]},
                ]},
                {"id": "11079", "label": "Кемеровская область — Кузбасс"},
                # одноимённые узлы: город и область
                {"id": "20", "label": "Двойник"},
                {"id": "21", "label": "Двойник"},
            ],
        }
    ]
}


class Region:
    def __init__(self, name, match=(), rtype="campaign"):
        self.name, self.match, self.type = name, tuple(match), rtype
        self.region_id = None
        self.cities, self.screens, self.campaign_start, self.note = (), 0, None, ""


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.index = regions_mod.index_tree(TREE)

    def test_exact_label(self):
        hit = regions_mod.resolve_one(self.index, ["Ярославская область"])
        self.assertEqual(hit["id"], "10841")

    def test_region_not_city(self):
        # Решение зафиксировано на уровне области: Тюмень (55) брать нельзя
        self.assertEqual(regions_mod.resolve_one(self.index, ["Тюменская область"])["id"], "11176")

    def test_alias_used_when_label_differs(self):
        hit = regions_mod.resolve_one(self.index, ["Кемеровская область", "Кемеровская область — Кузбасс"])
        self.assertEqual(hit["id"], "11079")

    def test_yo_and_case_are_ignored(self):
        self.assertEqual(regions_mod.resolve_one(self.index, ["орловская Область"])["id"], "10645")

    def test_ambiguity_is_reported_not_guessed(self):
        hit = regions_mod.resolve_one(self.index, ["Двойник"])
        self.assertIsNone(hit["id"])
        self.assertEqual({c["id"] for c in hit["candidates"]}, {"20", "21"})

    def test_dump_is_loadable(self):
        import yaml
        items = [regions_mod.region_to_dict(Region("Ярославская область"), "10841", "Ярославская область")]
        text = regions_mod.dump_regions({"level": "oblast"}, items)
        self.assertTrue(text.startswith("# Справочник регионов"))
        data = yaml.safe_load(text)
        self.assertEqual(data["regions"][0]["region_id"], "10841")


if __name__ == "__main__":
    unittest.main()
