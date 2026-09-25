import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import collect_eats


def response(payload, code=0):
    return mock.Mock(returncode=code, stdout=json.dumps(payload), stderr="")


class PlatformAiTests(unittest.TestCase):
    def test_normalizes_answer_and_citations(self):
        with mock.patch.object(collect_eats, "_resolve_command", return_value="opencli"), mock.patch.object(
            collect_eats.subprocess, "run", return_value=response({
                "answer": "今晚可以吃湘菜，推荐宁海食府和江南小馆。",
                "sources": [
                    {"title": "宁海食府探店", "author": "本地人", "url": "https://www.xiaohongshu.com/explore/a1"},
                    {"title": "无链接来源"},
                ],
            }),
        ) as run:
            result = collect_eats.ask_platform_ai("宁波海曙区今晚吃什么")

        self.assertTrue(result["ok"])
        lead = result["lead"]
        self.assertEqual(lead["platform"], "xiaohongshu")
        self.assertTrue(lead["untrusted"])
        self.assertEqual(lead["sources"][0]["url"], "https://www.xiaohongshu.com/explore/a1")
        self.assertTrue(lead["sources"][0]["verifiable"])
        self.assertFalse(lead["sources"][1]["verifiable"])
        self.assertIn("宁海食府探店", lead["store_candidates"])
        command = " ".join(run.call_args.args[0])
        self.assertIn("xiaohongshu ask", command)
        self.assertIn("--source-limit 10", command)

    def test_failure_is_explicit_and_does_not_raise(self):
        with mock.patch.object(collect_eats, "_resolve_command", return_value="opencli"), mock.patch.object(
            collect_eats.subprocess, "run", side_effect=subprocess.TimeoutExpired("opencli", 120)
        ):
            result = collect_eats.ask_platform_ai("杭州滨江区今晚吃什么")
        self.assertFalse(result["ok"])
        self.assertEqual(result["degraded"], "platform_ai_failed")
        self.assertEqual(result["reason"], "timeout")

    def test_unsupported_platform_does_not_call_opencli(self):
        with mock.patch.object(collect_eats, "_run_json") as run:
            result = collect_eats.ask_platform_ai("今晚吃什么", platform="unknown")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unsupported_platform")
        run.assert_not_called()

    def test_verifies_all_citations_and_keeps_independent_sources(self):
        lead = {
            "platform": "xiaohongshu",
            "store_candidates": ["宁海食府"],
            "sources": [
                {
                    "title": "宁海食府探店",
                    "author": "甲",
                    "url": "https://www.xiaohongshu.com/explore/a1",
                    "date": "2026-09-20",
                    "verifiable": True,
                },
                {
                    "title": "宁海食府实测",
                    "author": "乙",
                    "url": "https://www.xiaohongshu.com/explore/a2",
                    "date": "2026-09-18",
                    "verifiable": True,
                },
            ],
            "answer": "推荐宁海食府",
            "untrusted": True,
        }
        with mock.patch.object(
            collect_eats,
            "_read_note",
            side_effect=[
                {"ok": True, "data": {"desc": "招牌海鲜，晚上营业"}},
                {"ok": True, "data": {"content": "人均适中，排队较少"}},
            ],
        ) as read:
            result = collect_eats.verify_platform_lead(lead)

        self.assertTrue(result["ok"])
        verified = result["lead"]["sources"]
        self.assertEqual(len(verified), 2)
        self.assertEqual([source["url"] for source in verified], [
            "https://www.xiaohongshu.com/explore/a1",
            "https://www.xiaohongshu.com/explore/a2",
        ])
        self.assertEqual(verified[0]["content"], "招牌海鲜，晚上营业")
        self.assertEqual(verified[1]["content"], "人均适中，排队较少")
        self.assertTrue(all(source["verified"] for source in verified))
        self.assertTrue(result["lead"]["untrusted"])
        self.assertEqual(read.call_count, 2)

    def test_invalid_and_unavailable_citations_are_degraded_evidence(self):
        lead = {
            "platform": "xiaohongshu",
            "store_candidates": ["某店"],
            "sources": [
                {"title": "无链接来源", "author": "甲", "url": None, "date": None, "verifiable": False},
                {"title": "失效笔记", "author": "乙", "url": "https://example.com/missing", "date": "2026-09-01", "verifiable": True},
            ],
            "answer": "某店",
            "untrusted": True,
        }
        with mock.patch.object(
            collect_eats,
            "_read_note",
            return_value={"ok": False, "error": "timeout", "message": "timed out"},
        ) as read:
            result = collect_eats.verify_platform_lead(lead)

        self.assertTrue(result["ok"])
        sources = result["lead"]["sources"]
        self.assertFalse(sources[0]["verified"])
        self.assertEqual(sources[0]["degraded"], "invalid_citation")
        self.assertFalse(sources[1]["verified"])
        self.assertEqual(sources[1]["degraded"], "source_unavailable")
        self.assertEqual(result["lead"]["degraded"], "platform_source_verification")
        self.assertEqual(result["summary"], {"total": 2, "verified": 0, "degraded": 2})
        read.assert_called_once_with("https://example.com/missing", collect_eats.DEFAULT_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
