"""T4（#5）jev 素材判断点客户端测试：mock transport，全程不联网。

- 六条降级路径（无 key / 超时 / 5xx / 4xx / 坏回包 / 超限）各自断言
  reason 值 + 不抛异常
- 默认开启：无 env → enabled=True；JEV_POINT_MATERIAL=0 → 关闭零调用
- 限次：第 N+1 次不发 HTTP → degraded="limit_reached"
- score_candidate：达标 adopted=True / 低分 adopted=False
- 回包契约：payload 必含 model:"jev-latest" + questions.credibility.type=="score"
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import jev_client  # noqa: E402
from scripts.jev_client import JevClient  # noqa: E402

KEY_ENV_NAMES = (
    "TYPESAFE_API_KEY",
    "JEV_API_KEY",
    "JEV_ENV_FILE",
    "JEV_POINT_MATERIAL",
    "JEV_CALL_LIMIT",
    "JEV_SCORE_THRESHOLD",
)


def clean_env(**extra):
    """清掉 jev 相关 env（隔离宿主机环境）再注入 extra。"""
    env = {k: v for k, v in os.environ.items() if k not in KEY_ENV_NAMES}
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=True)


def ok_body(score):
    return 200, json.dumps({"answers": {"credibility": score}})


class RecordingTransport:
    """假 transport：记录 payload，按次序返回 (status, body) 或抛异常。

    结果列表耗尽后重复最后一项，便于限次测试。
    """

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, method, url, payload, headers, timeout):
        self.calls.append(json.loads(payload.decode("utf-8")))
        result = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


CANDIDATE = {
    "name": "方中山胡辣汤",
    "category": "汤食",
    "description": "来郑州必喝，配葛记焖饼",
    "note_source": {"title": "郑州必吃", "author": "x"},
    "image": {"url": "img/abc.webp", "source": "xiaohongshu", "credibility": "untrusted",
              "alt_description": "胡辣汤成品图"},
}


class EnableFlagTests(unittest.TestCase):
    def test_default_enabled(self):
        with clean_env():
            self.assertTrue(jev_client.material_point_enabled())

    def test_zero_disables(self):
        with clean_env(JEV_POINT_MATERIAL="0"):
            self.assertFalse(jev_client.material_point_enabled())

    def test_one_keeps_enabled(self):
        with clean_env(JEV_POINT_MATERIAL="1"):
            self.assertTrue(jev_client.material_point_enabled())


class DegradePathTests(unittest.TestCase):
    def _run(self, transport):
        with clean_env():
            client = JevClient(api_key="k", transport=transport)
            result = client.score("测试素材")
        return client, result

    def test_no_key_zero_http(self):
        transport = RecordingTransport([ok_body(9)])
        with clean_env():
            client = JevClient(api_key="", transport=transport)
            result = client.score("测试素材")
        self.assertEqual(transport.calls, [])
        self.assertEqual(client.calls_used, 0)
        self.assertTrue(result["degraded"])
        self.assertIsNone(result["score"])
        self.assertEqual(result["reason"], jev_client.NO_KEY)

    def test_timeout_retries_then_degrades(self):
        transport = RecordingTransport([TimeoutError(), TimeoutError()])
        client, result = self._run(transport)
        self.assertEqual(len(transport.calls), 2)  # 首次 + 重试 1 次
        self.assertEqual(result["reason"], jev_client.TIMEOUT)
        self.assertTrue(client.degraded)

    def test_5xx_retries_then_degrades(self):
        transport = RecordingTransport([(500, "boom"), (503, "boom")])
        client, result = self._run(transport)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(result["reason"], jev_client.HTTP_5XX)

    def test_4xx_no_retry(self):
        transport = RecordingTransport([(422, '{"detail":"model required"}')])
        client, result = self._run(transport)
        self.assertEqual(len(transport.calls), 1)  # 4xx 不重试
        self.assertEqual(result["reason"], jev_client.HTTP_4XX)

    def test_bad_body_json(self):
        transport = RecordingTransport([(200, "not json at all")])
        _, result = self._run(transport)
        self.assertEqual(result["reason"], jev_client.INVALID_RESPONSE)

    def test_bad_body_shape(self):
        transport = RecordingTransport([(200, '{"unexpected": true}'),
                                        (200, '{"answers": {"credibility": "NaN?"}}')])
        _, result = self._run(transport)
        self.assertEqual(result["reason"], jev_client.INVALID_RESPONSE)

    def test_network_error(self):
        transport = RecordingTransport([OSError("conn refused"), OSError()])
        _, result = self._run(transport)
        self.assertEqual(result["reason"], jev_client.NETWORK)

    def test_success_score_clamped(self):
        transport = RecordingTransport([ok_body(88)])
        _, result = self._run(transport)
        self.assertFalse(result["degraded"])
        self.assertEqual(result["score"], 10.0)  # 钳到 0-10


class LimitTests(unittest.TestCase):
    def test_nth_call_triggers_limit_reached_without_http(self):
        transport = RecordingTransport([ok_body(9)])
        with clean_env():
            client = JevClient(api_key="k", limit=2, transport=transport)
            self.assertFalse(client.score("a")["degraded"])
            self.assertFalse(client.score("b")["degraded"])
            result = client.score("c")  # 第 3 次：不发 HTTP
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(client.calls_used, 2)
        self.assertEqual(result["reason"], jev_client.LIMIT_REACHED)
        self.assertTrue(client.degraded)


class PayloadContractTests(unittest.TestCase):
    def test_payload_has_model_and_score_question(self):
        transport = RecordingTransport([ok_body(9)])
        with clean_env():
            JevClient(api_key="k", transport=transport).score("素材")
        payload = transport.calls[0]
        self.assertEqual(payload["model"], "jev-latest")
        self.assertEqual(payload["questions"]["credibility"]["type"], "score")
        self.assertEqual(payload["state"], "素材")
        self.assertNotIn("Authorization", json.dumps(payload))  # key 只在 header


class ScoreCandidateTests(unittest.TestCase):
    def test_adopted_when_score_meets_threshold(self):
        transport = RecordingTransport([ok_body(2.55)])  # raw 0-3 档，展示分 2.55/3*10=8.5
        with clean_env():
            client = JevClient(api_key="k", transport=transport)
            result = jev_client.score_candidate(client, CANDIDATE)
        self.assertEqual(result["jev_score"], 8.5)
        self.assertTrue(result["adopted"])
        self.assertNotIn("degraded", result)
        # 评分描述由候选字段拼成
        state = transport.calls[0]["state"]
        self.assertIn("方中山胡辣汤", state)
        self.assertIn("胡辣汤成品图", state)

    def test_rejected_when_score_below_threshold(self):
        transport = RecordingTransport([ok_body(1.2)])  # raw 1.2 -> 展示分 4.0
        with clean_env():
            client = JevClient(api_key="k", transport=transport)
            result = jev_client.score_candidate(client, CANDIDATE)
        self.assertAlmostEqual(result["jev_score"], 4.0, places=6)
        self.assertFalse(result["adopted"])

    def test_disabled_zero_calls(self):
        transport = RecordingTransport([ok_body(9)])
        with clean_env(JEV_POINT_MATERIAL="0"):
            client = JevClient(api_key="k", transport=transport)
            result = jev_client.score_candidate(client, CANDIDATE)
        self.assertEqual(transport.calls, [])  # 关闭时零调用
        self.assertIsNone(result["jev_score"])
        self.assertFalse(result["adopted"])
        self.assertEqual(result["degraded"], jev_client.DISABLED)

    def test_degraded_passthrough(self):
        transport = RecordingTransport([(500, "boom"), (500, "boom")])
        with clean_env():
            client = JevClient(api_key="k", transport=transport)
            result = jev_client.score_candidate(client, CANDIDATE)
        self.assertIsNone(result["jev_score"])
        self.assertFalse(result["adopted"])
        self.assertEqual(result["degraded"], jev_client.HTTP_5XX)

    def test_candidate_without_image_still_scores(self):
        transport = RecordingTransport([ok_body(2.1)])  # raw 2.1 -> 展示分 7.0，恰好达阈值
        with clean_env():
            client = JevClient(api_key="k", transport=transport)
            result = jev_client.score_candidate(
                client, {"name": "葛记焖饼", "category": "面食", "description": "配汤吃"}
            )
        self.assertTrue(result["adopted"])  # 7 >= 7 阈值边界
        self.assertAlmostEqual(result["jev_score"], 7.0, places=6)


class SummaryTests(unittest.TestCase):
    def test_success_shape(self):
        transport = RecordingTransport([ok_body(9), ok_body(3)])
        with clean_env():
            client = JevClient(api_key="k", limit=20, transport=transport)
            jev_client.score_candidate(client, CANDIDATE)
            jev_client.score_candidate(client, CANDIDATE)
            summary = jev_client.jev_summary(client, adopted=1)
        self.assertEqual(
            summary,
            {"ok": True, "calls": 2, "limit": 20, "threshold": 7.0,
             "adopted": 1, "degraded": False, "reason": None},
        )

    def test_disabled_shape(self):
        transport = RecordingTransport([ok_body(9)])
        with clean_env(JEV_POINT_MATERIAL="0"):
            client = JevClient(api_key="k", transport=transport)
            summary = jev_client.jev_summary(client, adopted=0)
        self.assertEqual(
            summary,
            {"ok": False, "calls": 0, "limit": 20, "threshold": 7.0,
             "adopted": 0, "degraded": True, "reason": "disabled"},
        )

    def test_degraded_shape(self):
        transport = RecordingTransport([TimeoutError(), TimeoutError()])
        with clean_env():
            client = JevClient(api_key="k", transport=transport)
            jev_client.score_candidate(client, CANDIDATE)
            summary = jev_client.jev_summary(client, adopted=0)
        self.assertFalse(summary["ok"])
        self.assertTrue(summary["degraded"])
        self.assertEqual(summary["reason"], jev_client.TIMEOUT)


if __name__ == "__main__":
    unittest.main()
