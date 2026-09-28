import json
import subprocess
import sys
import threading
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
        self.assertNotIn("answer", lead)
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

    def test_discovery_requires_ai_answer_and_citations(self):
        with mock.patch.object(
            collect_eats,
            "_run_json",
            return_value={"ok": True, "data": {"commands": [
                {"name": "ask", "columns": ["answer", "sources"]},
            ]}},
        ) as run:
            result = collect_eats.discover_platform_capabilities()

        self.assertEqual(result, {"capabilities": ["xiaohongshu"], "failures": []})
        self.assertEqual(run.call_args.args[0], [
            "opencli", "xiaohongshu", "--help", "-f", "json",
        ])
        with mock.patch.object(
            collect_eats,
            "_run_json",
            return_value={"ok": True, "data": {"commands": [
                {"name": "ask", "columns": ["answer"]},
            ]}},
        ):
            result = collect_eats.discover_platform_capabilities()
        self.assertEqual(result["capabilities"], [])
        self.assertEqual(result["failures"][0]["reason"], "ask_unavailable")

    def test_scheduler_caps_parallel_calls_and_keeps_other_platforms_on_failure(self):
        barrier = threading.Barrier(3)
        calls = []
        lock = threading.Lock()

        def ask(_query, platform, timeout):
            with lock:
                calls.append(platform)
            barrier.wait(timeout=2)
            if platform == "two":
                return {"ok": False, "reason": "timeout"}
            return {"ok": True, "lead": {"platform": platform, "sources": []}}

        def verify(lead, timeout):
            return {"lead": lead, "summary": {"total": 0, "verified": 0, "degraded": 0}}

        with mock.patch.object(
            collect_eats,
            "discover_platform_capabilities",
            return_value={"capabilities": ["one", "two", "three", "four"], "failures": []},
        ), mock.patch.object(collect_eats, "ask_platform_ai", side_effect=ask), mock.patch.object(
            collect_eats, "verify_platform_lead", side_effect=verify
        ):
            result = collect_eats.collect_platform_leads("宁波")

        metadata = result["metadata"]
        self.assertEqual(metadata["calls"], 3)
        self.assertEqual(metadata["successes"], 2)
        self.assertEqual(metadata["failures"][0]["reason"], "timeout")
        self.assertEqual(set(calls), {"one", "two", "three"})

    def test_explicit_platform_runs_before_automatic_discovery(self):
        order = []

        def discover(platforms, timeout):
            order.append(("discover", platforms))
            return {"capabilities": ["xiaohongshu"], "failures": []}

        def ask(_query, platform, timeout):
            order.append(("ask", platform))
            return {"ok": True, "lead": {"platform": platform, "sources": []}}

        with mock.patch.object(
            collect_eats, "discover_platform_capabilities", side_effect=discover
        ), mock.patch.object(collect_eats, "ask_platform_ai", side_effect=ask), mock.patch.object(
            collect_eats,
            "verify_platform_lead",
            side_effect=lambda lead, timeout: {
                "lead": lead,
                "summary": {"total": 0, "verified": 0, "degraded": 0},
            },
        ):
            result = collect_eats.collect_platform_leads("宁波", preferred_platform="小红书")

        self.assertEqual(order, [("discover", ["xiaohongshu"]), ("ask", "xiaohongshu")])
        self.assertEqual(result["metadata"]["calls"], 1)

    def test_citationless_store_stays_out_of_candidates_but_failed_source_is_retained(self):
        lead = {
            "platform": "xiaohongshu",
            "direction": "湘菜",
            "store_candidates": ["宁海食府"],
            "sources": [],
            "untrusted": True,
        }
        self.assertEqual(collect_eats.platform_lead_candidates([lead]), [])
        lead["sources"] = [{
            "platform": "xiaohongshu",
            "title": "宁海食府探店",
            "author": "本地人",
            "url": "https://www.xiaohongshu.com/explore/a1",
            "verified": False,
            "degraded": "source_unavailable",
        }]
        candidate = collect_eats.platform_lead_candidates([lead])[0]
        self.assertTrue(candidate["platform_ai_unverified"])
        self.assertFalse(candidate["sources"][0]["verified"])

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
                {"ok": True, "data": {"desc": "宁海食府：招牌海鲜，晚上营业"}},
                {"ok": True, "data": {"content": "宁海食府实测：人均适中，排队较少"}},
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
        self.assertEqual(verified[0]["content"], "宁海食府：招牌海鲜，晚上营业")
        self.assertEqual(verified[1]["content"], "宁海食府实测：人均适中，排队较少")
        self.assertTrue(all(source["verified"] for source in verified))
        self.assertTrue(result["lead"]["untrusted"])
        self.assertEqual(read.call_count, 2)

    def test_invalid_and_unavailable_citations_are_degraded_evidence(self):
        lead = {
            "platform": "xiaohongshu",
            "store_candidates": ["某店"],
            "sources": [
                {"title": "无链接来源", "author": "甲", "url": None, "date": None, "verifiable": False},
                {"title": "失效笔记", "author": "乙", "url": "https://www.xiaohongshu.com/explore/missing", "date": "2026-09-01", "verifiable": True},
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
        read.assert_called_once_with("https://www.xiaohongshu.com/explore/missing", collect_eats.DEFAULT_TIMEOUT)

    def test_foreign_citation_is_rejected_without_reading_it(self):
        lead = {
            "platform": "xiaohongshu",
            "store_candidates": ["某店"],
            "sources": [{"url": "https://example.com/note"}],
        }
        with mock.patch.object(collect_eats, "_read_note") as read:
            result = collect_eats.verify_platform_lead(lead)

        self.assertEqual(result["lead"]["sources"][0]["degraded"], "invalid_citation")
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
