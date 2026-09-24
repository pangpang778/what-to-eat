"""T5（#6）拍板引擎测试：mock 采集与 jev，全程不联网。

- 幸福路径：verdict 完整、低分候选转 JEV alert、alternates_hint ≤3、
  reason 引用真实 jev 分数（事实铁律）
- 全拒路径：jev 全拒 → 启发式孪生 + all_candidates_rejected + degraded
- 采集失败路径：collect ok=False → 兜底 verdict + degraded
- alternate 模式：排除上次 pick
- 记忆联动：忌口/近 3 天/低权重过滤生效进 verdict 链路
- 降级记录完整性：每条 degraded 路径在 verdict.degraded[] 有对应记录
- jev 全开夹具回归：tests/fixtures/verdict_jev_full.json
"""

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import decide  # noqa: E402
from scripts import memory as memory_mod  # noqa: E402

TODAY = date(2026, 9, 23)
LOCATION = "郑州"
HAPPY = json.loads(
    (Path("tests") / "fixtures" / "verdict_happy.json").read_text(encoding="utf-8")
)
FULL = json.loads(
    (Path("tests") / "fixtures" / "verdict_jev_full.json").read_text(encoding="utf-8")
)


def fresh_candidates():
    """深拷贝 happy 夹具候选（打分时会原地盖 jev_score/adopted）。"""
    return json.loads(json.dumps(HAPPY["candidates"]))


class FakeJev:
    """按候选顺序吐分数的假 JevClient；None 表示该次调用降级 no_key。"""

    def __init__(self, scores):
        self.scores = list(scores)
        self.index = 0
        self.limit = 20
        self.threshold = 7.0
        self.calls_used = 0
        self.degraded = False
        self.reason = None

    def score(self, state):
        self.calls_used += 1
        value = self.scores[min(self.index, len(self.scores) - 1)]
        self.index += 1
        if value is None:
            self.degraded = True
            self.reason = "no_key"
            return {"ok": False, "score": None, "degraded": True, "reason": "no_key"}
        return {"ok": True, "score": value, "degraded": False, "reason": None}


def run(cands, scores, memory=None, request=None, enabled=True):
    """跑管线：mock collect 与 JevClient；记忆写临时文件。"""
    mem = memory if memory is not None else {
        "eaten_log": [], "taboos": [], "weights": {}
    }
    request = request or {"location": LOCATION, "constraints": {}, "mode": "decide"}
    with tempfile.TemporaryDirectory() as tmp:
        mem_path = Path(tmp) / "memory.json"
        memory_mod.save_memory(mem_path, mem)
        with mock.patch.object(
            decide.collect_eats, "collect",
            return_value={"ok": True, "candidates": cands, "notes": 3, "images": 4,
                          "tool": "opencli"},
        ), mock.patch.object(
            decide.jev_client, "material_point_enabled", return_value=enabled
        ), mock.patch.object(
            decide.jev_client, "JevClient", return_value=FakeJev(scores)
        ):
            result = decide.run_pipeline(request, tmp, mem_path, today=TODAY)
        recorded = memory_mod.load_memory(mem_path)
    return result, recorded


class HappyPathTests(unittest.TestCase):
    def test_verdict_complete_and_grounded(self):
        result, recorded = run(fresh_candidates(), [8.7, 9.1, 6.2, 8.4])
        verdict = result["verdict"]
        self.assertEqual(verdict["pick"]["name"], "方中山胡辣汤")
        # 事实铁律：理由引用真实 jev 分数
        self.assertIn("9.1", verdict["reason"])
        self.assertIn("近期吃过", verdict["reason"])
        # untrusted 铁律：verdict 图片带 untrusted 标记且 adopted
        self.assertIn("untrusted", verdict["image"]["credibility"])
        self.assertTrue(verdict["image"]["adopted"])
        self.assertEqual(verdict["image"]["jev_score"], 9.1)
        # alternates ≤3 且为次优（8.7 > 8.4）
        self.assertEqual(verdict["alternates_hint"], ["葛记焖饼", "合记烩面"])
        self.assertLessEqual(len(verdict["alternates_hint"]), 3)
        self.assertEqual(verdict["degraded"], [])
        self.assertNotIn("all_candidates_rejected", verdict)
        # 候选被原地盖戳（schema image.jev_score/adopted）
        stamped = {c["name"]: c["image"] for c in result["candidates"]}
        self.assertTrue(stamped["方中山胡辣汤"]["adopted"])
        self.assertFalse(stamped["京都老蔡记"]["adopted"])
        self.assertEqual(stamped["京都老蔡记"]["jev_score"], 6.2)
        # pipeline 披露
        self.assertTrue(result["pipeline"]["collect"]["ok"])
        self.assertEqual(result["pipeline"]["jev"]["adopted"], 3)
        # 拍板自动记录
        self.assertIn(
            "方中山胡辣汤", [e["pick"] for e in recorded["eaten_log"]]
        )

    def test_low_score_candidate_becomes_jev_alert(self):
        result, _ = run(fresh_candidates(), [8.7, 9.1, 6.2, 8.4])
        jev_alerts = [a for a in result["alerts"] if a["source"] == "JEV"]
        self.assertEqual(len(jev_alerts), 1)
        alert = jev_alerts[0]
        self.assertEqual(alert["type"], "素材筛选")
        self.assertEqual(alert["level"], "advisory")
        self.assertIn("京都老蔡记", alert["title"])
        self.assertIn("6.2", alert["title"])

    def test_same_day_reruns_are_deterministic(self):
        first, _ = run(fresh_candidates(), [8.7, 9.1, 6.2, 8.4])
        second, _ = run(fresh_candidates(), [8.7, 9.1, 6.2, 8.4])
        self.assertEqual(
            first["verdict"]["pick"]["name"], second["verdict"]["pick"]["name"]
        )


class AllRejectedTests(unittest.TestCase):
    def test_all_rejected_goes_twin_with_flag(self):
        result, _ = run(fresh_candidates(), [3.0, 2.0, 1.0, 0.5])
        verdict = result["verdict"]
        self.assertTrue(verdict["all_candidates_rejected"])
        self.assertIn("all_candidates_rejected: 启发式孪生", verdict["degraded"])
        # 低分素材不入 verdict——孪生拍板不带图
        self.assertNotIn("image", verdict)
        self.assertIn(verdict["pick"]["name"],
                      {c["name"] for c in HAPPY["candidates"]})
        self.assertIn("jev 素材筛选", verdict["reason"])
        self.assertEqual(result["pipeline"]["jev"]["adopted"], 0)


class CollectFailedTests(unittest.TestCase):
    def test_collect_failure_falls_back(self):
        request = {"location": LOCATION, "constraints": {}, "mode": "decide"}
        with tempfile.TemporaryDirectory() as tmp:
            mem_path = Path(tmp) / "memory.json"
            with mock.patch.object(
                decide.collect_eats, "collect",
                return_value={"ok": False, "candidates": [], "notes": 0,
                              "images": 0, "tool": "opencli",
                              "degraded": "collect_failed"},
            ), mock.patch.object(
                decide.jev_client, "material_point_enabled", return_value=True
            ):
                result = decide.run_pipeline(request, tmp, mem_path, today=TODAY)
        verdict = result["verdict"]
        self.assertIn("collect_failed: 启发式孪生", verdict["degraded"])
        self.assertIn("非实时采集", verdict["reason"])
        self.assertTrue(any(a["source"] == "RULE" for a in result["alerts"]))
        self.assertFalse(result["pipeline"]["collect"]["ok"])
        self.assertTrue(result["pipeline"]["jev"]["degraded"])


class AlternateModeTests(unittest.TestCase):
    def test_alternate_excludes_last_pick(self):
        memory = {
            "eaten_log": [{"location": LOCATION, "pick": "方中山胡辣汤",
                           "date": (TODAY - timedelta(days=5)).isoformat()}],
            "taboos": [], "weights": {},
        }
        request = {"location": LOCATION, "constraints": {}, "mode": "alternate"}
        result, _ = run(fresh_candidates(), [8.7, 9.1, 6.2, 8.4], memory=memory,
                        request=request)
        verdict = result["verdict"]
        self.assertEqual(verdict["pick"]["name"], "葛记焖饼")
        self.assertNotEqual(verdict["pick"]["name"], "方中山胡辣汤")
        self.assertIn("已避开上一次拍板「方中山胡辣汤」", verdict["reason"])

    def test_decide_mode_keeps_top_even_if_eaten_long_ago(self):
        memory = {
            "eaten_log": [{"location": LOCATION, "pick": "方中山胡辣汤",
                           "date": (TODAY - timedelta(days=5)).isoformat()}],
            "taboos": [], "weights": {},
        }
        result, _ = run(fresh_candidates(), [8.7, 9.1, 6.2, 8.4], memory=memory)
        self.assertEqual(result["verdict"]["pick"]["name"], "方中山胡辣汤")


class MemoryLinkTests(unittest.TestCase):
    def test_taboo_and_recent_filter_chain_into_verdict(self):
        memory = {
            "eaten_log": [{"location": LOCATION, "pick": "葛记焖饼",
                           "date": TODAY.isoformat()}],
            "taboos": ["烩面"], "weights": {},
        }
        result, recorded = run(fresh_candidates(), [8.7, 9.1, 6.2, 8.4],
                               memory=memory)
        verdict = result["verdict"]
        # 合记烩面（忌口）+ 葛记焖饼（近 3 天）被剔 → 只剩方中山可拍
        self.assertEqual(verdict["pick"]["name"], "方中山胡辣汤")
        rule_alerts = [a for a in result["alerts"] if a["source"] == "RULE"]
        self.assertEqual(len(rule_alerts), 1)
        self.assertEqual(rule_alerts[0]["type"], "记忆过滤")
        self.assertIn("合记烩面", rule_alerts[0]["detail"])
        self.assertIn("葛记焖饼", rule_alerts[0]["detail"])
        # 拍板后 eaten_log 追加，未污染旧记录
        picks = [e["pick"] for e in recorded["eaten_log"]]
        self.assertEqual(picks.count("方中山胡辣汤"), 1)

    def test_low_weight_candidate_filtered(self):
        memory = {"eaten_log": [], "taboos": [],
                  "weights": {"方中山胡辣汤": 0.4}}
        result, _ = run(fresh_candidates(), [8.7, 9.1, 6.2, 8.4], memory=memory)
        verdict = result["verdict"]
        self.assertEqual(verdict["pick"]["name"], "葛记焖饼")
        self.assertIn("方中山胡辣汤",
                      [a["detail"] for a in result["alerts"]
                       if a["source"] == "RULE" and a["type"] == "记忆过滤"][0])

    def test_memory_filter_empties_pool_to_fallback(self):
        memory = {
            "eaten_log": [{"location": LOCATION, "pick": "葛记焖饼",
                           "date": TODAY.isoformat()},
                          {"location": LOCATION, "pick": "方中山胡辣汤",
                           "date": TODAY.isoformat()},
                          {"location": LOCATION, "pick": "合记烩面",
                           "date": TODAY.isoformat()}],
            "taboos": [], "weights": {},
        }
        result, _ = run(fresh_candidates(), [8.7, 9.1, 6.2, 8.4], memory=memory)
        verdict = result["verdict"]
        self.assertIn("memory_filtered_all: 启发式孪生", verdict["degraded"])
        self.assertIn("非实时采集", verdict["reason"])


class DegradedRecordTests(unittest.TestCase):
    def test_jev_disabled_path(self):
        result, _ = run(fresh_candidates(), [8.7, 9.1, 6.2, 8.4], enabled=False)
        verdict = result["verdict"]
        self.assertIn("jev_disabled: 宿主 AI 复核", verdict["degraded"])
        self.assertEqual(result["pipeline"]["jev"]["reason"], "disabled")
        # 未经 jev 达标的图不入 verdict
        self.assertNotIn("image", verdict)
        self.assertIn(verdict["pick"]["name"],
                      {c["name"] for c in HAPPY["candidates"]})

    def test_jev_unavailable_no_key_path(self):
        result, _ = run(fresh_candidates(), [None, 9.1, 6.2, 8.4])
        verdict = result["verdict"]
        self.assertIn("jev_no_key: 宿主 AI 复核", verdict["degraded"])
        self.assertEqual(result["pipeline"]["jev"]["reason"], "no_key")
        # 降级前已评分的 2 个候选真实达标（诚实披露）
        self.assertEqual(result["pipeline"]["jev"]["adopted"], 2)
        # 降级候选不保留达标标记
        stripped = {c["name"]: c["image"] for c in result["candidates"]}
        self.assertNotIn("adopted", stripped["葛记焖饼"])


class JevFullFixtureTests(unittest.TestCase):
    """「jev 全开」夹具：happy + pipeline.jev 全字段（ok/calls/limit/
    threshold/adopted/degraded/reason）。"""

    def test_pipeline_jev_full_fields(self):
        jev = FULL["pipeline"]["jev"]
        self.assertEqual(
            set(jev.keys()),
            {"ok", "calls", "limit", "threshold", "adopted", "degraded", "reason"},
        )
        self.assertTrue(jev["ok"])
        self.assertFalse(jev["degraded"])
        self.assertIsNone(jev["reason"])
        self.assertEqual(jev["adopted"], 3)

    def test_verdict_contract(self):
        verdict = FULL["verdict"]
        self.assertIn("untrusted", verdict["image"]["credibility"])
        self.assertIn("9.1", verdict["reason"])
        self.assertEqual(len(verdict["alternates_hint"]), 2)
        self.assertEqual(verdict["degraded"], [])
        adopted = [c for c in FULL["candidates"] if c["image"]["adopted"]]
        self.assertEqual(len(adopted), 3)


if __name__ == "__main__":
    unittest.main()


class IntentV2Tests(unittest.TestCase):
    """反问轮 v2：意图进 jev 评分描述 + 菜系加权（spec #9）。"""

    def test_intent_context_passed_to_jev(self):
        # party/budget/location_anchor 合成意图上下文，附加在评分描述后
        captured = []

        def fake_score(state):
            captured.append(state)
            return {"ok": True, "score": 8.0, "degraded": False, "reason": None}

        request = {
            "location": LOCATION,
            "constraints": {"party": "堂食 2 人", "budget": "人均 50", "cuisine_pref": "湘菜"},
            "mode": "decide",
        }
        mem = {"eaten_log": [], "taboos": [], "weights": {}}
        with tempfile.TemporaryDirectory() as tmp:
            mem_path = Path(tmp) / "memory.json"
            memory_mod.save_memory(mem_path, mem)
            with mock.patch.object(
                decide.collect_eats, "collect",
                return_value={"ok": True, "candidates": fresh_candidates(),
                              "notes": 3, "images": 4, "tool": "opencli"},
            ), mock.patch.object(
                decide.jev_client, "JevClient",
                type("FakeJev2", (object,), {
                    "limit": 20, "threshold": 7.0, "degraded": False, "reason": None,
                    "calls_used": 0,
                    "score": lambda self, state: fake_score(state),
                    "__call__": lambda self: self,
                })(),
            ):
                decide.run_pipeline(request, tmp, mem_path, today=TODAY)
        self.assertTrue(captured)
        for state in captured:
            self.assertIn("用餐场景/意图", state)
            self.assertIn("堂食 2 人", state)
            self.assertIn("人均 50", state)

    def test_cuisine_boost_ranks_matching_candidate_first(self):
        # cuisine_pref 命中的候选 ×1.5 加权，反超更高基础分的候选
        cands = fresh_candidates()
        cands[0]["description"] = "胡辣汤"
        cands[1]["description"] = "湘菜剁椒鱼头"
        request = {
            "location": LOCATION,
            "constraints": {"cuisine_pref": "湘菜"},
            "mode": "decide",
        }
        result, _ = run(cands, [9.1, 7.0, 6.0, 5.0], request=request)
        self.assertEqual(result["verdict"]["pick"]["name"], cands[1]["name"])

    def test_no_cuisine_pref_no_boost(self):
        # 无 cuisine_pref → 行为等价无意图（最高分第一）
        result, _ = run(fresh_candidates(), [8.7, 9.1, 6.2, 8.4])
        self.assertEqual(result["verdict"]["pick"]["name"], "方中山胡辣汤")
