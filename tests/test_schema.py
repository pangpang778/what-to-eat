"""T1（#2）回归基线：拍板 schema 契约与双夹具。

- verdict_happy.json：幸福路径（采集+jev 全成功，verdict 完整）
- verdict_minimal.json：最小降级（采集失败+jev 不可用，启发式孪生拍板）
- docs/SCHEMA.md 的字段表与本测试互为契约：新增字段必须先改文档再进夹具。
"""

import json
import unittest
from pathlib import Path

FIXTURES = Path("tests") / "fixtures"
HAPPY = json.loads((FIXTURES / "verdict_happy.json").read_text(encoding="utf-8"))
MINIMAL = json.loads((FIXTURES / "verdict_minimal.json").read_text(encoding="utf-8"))


def _is_ll(pair):
    return (
        isinstance(pair, (list, tuple))
        and len(pair) == 2
        and all(isinstance(v, (int, float)) for v in pair)
        and 73 <= pair[0] <= 136
        and 3 <= pair[1] <= 54
    )


class HappyFixtureTests(unittest.TestCase):
    def test_request_contract(self):
        req = HAPPY["request"]
        self.assertEqual(req["mode"], "decide")
        self.assertTrue(req["location"])
        self.assertIn("taboos", req["constraints"])

    def test_candidates_carry_untrusted_sources(self):
        self.assertEqual(len(HAPPY["candidates"]), 4)
        for c in HAPPY["candidates"]:
            self.assertTrue(c["name"], "candidate name required")
            self.assertTrue(c["category"], "category required")
            src = c["note_source"]
            for key in ("title", "author"):
                self.assertTrue(str(src.get(key, "")).strip(), key)
            img = c.get("image")
            if img:
                self.assertIn("untrusted", img["credibility"], c["name"])
                self.assertTrue(img["url"])
                for key in ("source", "alt_description"):
                    self.assertTrue(img.get(key), key)

    def test_jev_adoption_contract(self):
        adopted = [c for c in HAPPY["candidates"] if c["image"].get("adopted")]
        rejected = [c for c in HAPPY["candidates"] if not c["image"].get("adopted")]
        self.assertTrue(adopted)
        self.assertTrue(rejected)
        for c in adopted:
            self.assertGreaterEqual(c["image"]["jev_score"], 7.0)
        for c in rejected:
            self.assertLess(c["image"]["jev_score"], 7.0)

    def test_verdict_complete_and_grounded(self):
        v = HAPPY["verdict"]
        self.assertTrue(v["pick"]["name"])
        self.assertTrue(v["pick"]["category"])
        self.assertTrue(v["reason"])
        # 理由引用的 jev 分数必须与候选实际分数一致（事实铁律）
        score_in_reason = any(
            str(c["image"]["jev_score"]) in v["reason"]
            for c in HAPPY["candidates"] if c["image"].get("adopted")
        )
        self.assertTrue(score_in_reason, "reason must cite real schema data")
        self.assertEqual(v["pick"]["name"], "方中山胡辣汤")
        img = v["image"]
        self.assertIn("untrusted", img["credibility"])
        self.assertGreaterEqual(img["jev_score"], 7.0)
        self.assertEqual(v["alternates_hint"], ["葛记焖饼", "合记烩面"])
        self.assertEqual(v["degraded"], [])

    def test_memory_contract(self):
        m = HAPPY["memory"]
        for entry in m["eaten_log"]:
            self.assertRegex(entry["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn("香菜", m["taboos"])
        self.assertTrue(m["weights"])

    def test_pipeline_contract(self):
        p = HAPPY["pipeline"]
        self.assertTrue(p["collect"]["ok"])
        self.assertEqual(p["jev"]["limit"], 20)
        self.assertEqual(p["jev"]["threshold"], 7)
        self.assertEqual(p["jev"]["adopted"], 3)
        alerts = HAPPY["alerts"]
        self.assertEqual(alerts[0]["source"], "JEV")
        self.assertEqual(alerts[0]["level"], "advisory")


class MinimalFixtureTests(unittest.TestCase):
    def test_degradation_explicit(self):
        v = MINIMAL["verdict"]
        self.assertIn("collect_failed", " ".join(v["degraded"]))
        self.assertIn("jev_disabled", " ".join(v["degraded"]))
        self.assertFalse(v.get("image"))

    def test_no_candidates_but_verdict_stands(self):
        self.assertEqual(MINIMAL["candidates"], [])
        self.assertTrue(MINIMAL["verdict"]["pick"]["name"])
        self.assertEqual(MINIMAL["pipeline"]["jev"]["reason"], "no_key")

    def test_alerts_advisory_only(self):
        for a in MINIMAL.get("alerts", []):
            self.assertEqual(a["level"], "advisory")


if __name__ == "__main__":
    unittest.main()
