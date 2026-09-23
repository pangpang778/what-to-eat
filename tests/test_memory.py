"""T2（#3）记忆层测试：全部 tempfile，不联网。

覆盖验收项：冷启动 / 损坏 JSON 不覆盖原文件 / round-trip / 原子写 /
忌口过滤 / 近 3 天同 location 去重（异 location 不误杀）/ 低权重过滤 /
拍板自动 append（date 断言）/ 评分加减权与阈值联动。
"""

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import memory  # noqa: E402


def _cand(name, category="豫菜", description="一句话描述"):
    return {"name": name, "category": category, "description": description}


class LoadSaveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "memory.json"

    def test_cold_start_when_missing(self):
        m = memory.load_memory(self.path)
        self.assertEqual(m, {"eaten_log": [], "taboos": [], "weights": {}})

    def test_corrupt_json_cold_start_and_file_untouched(self):
        garbage = "{这不是合法 JSON"
        self.path.write_text(garbage, encoding="utf-8")
        m = memory.load_memory(self.path)
        self.assertEqual(m["eaten_log"], [])
        self.assertEqual(m["taboos"], [])
        self.assertEqual(m["weights"], {})
        self.assertEqual(self.path.read_text(encoding="utf-8"), garbage)

    def test_corrupt_json_top_level_not_dict(self):
        self.path.write_text('["array"]', encoding="utf-8")
        self.assertEqual(memory.load_memory(self.path)["weights"], {})

    def test_round_trip_and_manual_edit_keys_filled(self):
        m = {"eaten_log": [{"location": "郑州", "pick": "葛记焖饼", "date": "2026-09-21"}],
             "taboos": ["香菜"], "weights": {"葛记焖饼": 1.2}}
        memory.save_memory(self.path, m)
        loaded = memory.load_memory(self.path)
        self.assertEqual(loaded, m)
        # 用户手改缺 weights 键 → 降级补齐不崩
        self.path.write_text(
            json.dumps({"eaten_log": [], "taboos": ["花生"]}, ensure_ascii=False),
            encoding="utf-8",
        )
        loaded = memory.load_memory(self.path)
        self.assertEqual(loaded["taboos"], ["花生"])
        self.assertEqual(loaded["weights"], {})

    def test_atomic_write_no_tmp_left_and_chinese_readable(self):
        memory.save_memory(self.path, {"eaten_log": [], "taboos": ["香菜"], "weights": {}})
        self.assertFalse((Path(self._tmp.name) / "memory.json.tmp").exists())
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("香菜", text)  # ensure_ascii=False


def _mem_with(entries=(), taboos=(), weights=None):
    return {"eaten_log": list(entries), "taboos": list(taboos), "weights": weights or {}}


class FilterTests(unittest.TestCase):
    TODAY = date(2026, 9, 23)

    def test_taboo_filters_candidate_hit_in_any_field(self):
        cands = [_cand("羊肉烩面"), _cand("香菜拌牛肉"), _cand("焖饼", description="配香菜高汤")]
        eligible, note = memory.filter_candidates(
            cands, _mem_with(taboos=["香菜"]), "郑州", today=self.TODAY
        )
        self.assertEqual([c["name"] for c in eligible], ["羊肉烩面"])
        self.assertIn("忌口过滤", note)
        self.assertIn("香菜拌牛肉", note)
        self.assertIn("焖饼", note)

    def test_taboo_no_hit_keeps_all(self):
        eligible, note = memory.filter_candidates(
            [_cand("羊肉烩面")], _mem_with(taboos=["香菜"]), "郑州", today=self.TODAY
        )
        self.assertEqual(len(eligible), 1)
        self.assertEqual(note, "")

    def test_recent_same_location_filtered_other_location_kept(self):
        entries = [
            {"location": "郑州", "pick": "葛记焖饼", "date": "2026-09-22"},
            {"location": "西安", "pick": "葛记焖饼", "date": "2026-09-22"},
        ]
        eligible, note = memory.filter_candidates(
            [_cand("葛记焖饼")], _mem_with(entries=entries), "郑州", today=self.TODAY
        )
        self.assertEqual(eligible, [])
        self.assertIn("近 3 天同地点吃过", note)

    def test_recent_outside_window_not_filtered(self):
        entries = [{"location": "郑州", "pick": "葛记焖饼", "date": "2026-09-19"}]
        eligible, _ = memory.filter_candidates(
            [_cand("葛记焖饼")], _mem_with(entries=entries), "郑州", today=self.TODAY
        )
        self.assertEqual(len(eligible), 1)

    def test_recent_bad_date_entry_ignored(self):
        entries = [{"location": "郑州", "pick": "葛记焖饼", "date": "garbage"}]
        eligible, _ = memory.filter_candidates(
            [_cand("葛记焖饼")], _mem_with(entries=entries), "郑州", today=self.TODAY
        )
        self.assertEqual(len(eligible), 1)

    def test_low_weight_filtered_normal_weight_kept(self):
        mem = _mem_with(weights={"京都老蔡记": 0.4, "葛记焖饼": 1.2})
        eligible, note = memory.filter_candidates(
            [_cand("京都老蔡记"), _cand("葛记焖饼"), _cand("新店")],
            mem, "郑州", today=self.TODAY,
        )
        self.assertEqual([c["name"] for c in eligible], ["葛记焖饼", "新店"])
        self.assertIn("低权重剔除: 京都老蔡记", note)


class RecordAndRatingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "memory.json"

    def test_record_verdict_appends_with_date(self):
        memory.record_verdict(
            self.path, "郑州", {"pick": {"name": "方中山胡辣汤", "category": "早餐"}},
            today=date(2026, 9, 23),
        )
        m = memory.load_memory(self.path)
        self.assertEqual(
            m["eaten_log"],
            [{"location": "郑州", "pick": "方中山胡辣汤", "date": "2026-09-23"}],
        )
        # 第二次拍板继续 append
        memory.record_verdict(self.path, "西安", {"pick": {"name": "羊肉泡馍"}},
                              today=date(2026, 9, 24))
        self.assertEqual(len(memory.load_memory(self.path)["eaten_log"]), 2)

    def test_rating_up_adds_weight_caps_at_ceil(self):
        memory.apply_rating(self.path, "葛记焖饼", "up", today=date(2026, 9, 23))
        self.assertEqual(memory.load_memory(self.path)["weights"]["葛记焖饼"], 1.2)
        for _ in range(10):
            memory.apply_rating(self.path, "葛记焖饼", "up")
        self.assertEqual(memory.load_memory(self.path)["weights"]["葛记焖饼"], 2.0)

    def test_rating_down_subtracts_floors_at_zero(self):
        memory.apply_rating(self.path, "京都老蔡记", "down", today=date(2026, 9, 23))
        self.assertEqual(memory.load_memory(self.path)["weights"]["京都老蔡记"], 0.6)
        memory.apply_rating(self.path, "京都老蔡记", "down")
        memory.apply_rating(self.path, "京都老蔡记", "down")
        self.assertEqual(memory.load_memory(self.path)["weights"]["京都老蔡记"], 0.0)

    def test_down_rating_below_threshold_gets_filtered(self):
        memory.apply_rating(self.path, "京都老蔡记", "down", today=date(2026, 9, 23))
        memory.apply_rating(self.path, "京都老蔡记", "down")
        eligible, note = memory.filter_candidates(
            [_cand("京都老蔡记")], memory.load_memory(self.path), "郑州",
            today=date(2026, 9, 23),
        )
        self.assertEqual(eligible, [])
        self.assertIn("京都老蔡记", note)

    def test_rating_written_back_to_matching_log_entry(self):
        memory.record_verdict(self.path, "郑州", {"pick": {"name": "葛记焖饼"}},
                              today=date(2026, 9, 22))
        memory.apply_rating(self.path, "葛记焖饼", "up", location="郑州")
        entry = memory.load_memory(self.path)["eaten_log"][0]
        self.assertEqual(entry["rating"], "up")

    def test_invalid_rating_raises(self):
        with self.assertRaises(ValueError):
            memory.apply_rating(self.path, "葛记焖饼", "meh")


if __name__ == "__main__":
    unittest.main()
