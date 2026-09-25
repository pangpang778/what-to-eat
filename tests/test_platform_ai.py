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


if __name__ == "__main__":
    unittest.main()
