#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""server.py 的测试：重试预算、缓存、以及不联网的那部分 HTTP 层。

网络被替换掉，所以这里不需要 API key，也不会真的花钱。

    python3 test_server.py
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.request

import server


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(status, detail="boom"):
    return urllib.error.HTTPError(
        "https://example", status, "err", {}, io.BytesIO(detail.encode()))


class UrlopenRecorder:
    """假的 urlopen：按脚本依次抛出或返回，并记下每次的调用参数。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.timeouts = []

    def __call__(self, request, timeout=None):
        self.calls += 1
        self.timeouts.append(timeout)
        if not self.script:
            raise AssertionError("urlopen 被多调用了一次")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return FakeResponse(step)


ANSWER = {
    "model": "jev-test",
    "usage": {"input_tokens": 10, "output_tokens": 4},
    "answers": {"prediction": {"type": "choice", "choice": "rock",
                               "probabilities": {"rock": 0.9, "paper": 0.05, "scissors": 0.05}}},
}


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.original = urllib.request.urlopen
        self.original_cache_enabled = server.CACHE.enabled
        server.CACHE.enabled = lambda: False       # 这些测试要看到真实的网络行为
        server.time.sleep = lambda _s: None        # 不要把退避也等一遍

    def tearDown(self):
        urllib.request.urlopen = self.original
        server.CACHE.enabled = self.original_cache_enabled
        server.time.sleep = time.sleep

    def fake(self, script):
        recorder = UrlopenRecorder(script)
        urllib.request.urlopen = recorder
        return recorder

    def test_returns_on_the_first_success(self):
        recorder = self.fake([ANSWER])
        payload, _latency, cached = server.ask_jev("state", "key")
        self.assertEqual(payload["model"], "jev-test")
        self.assertFalse(cached)
        self.assertEqual(recorder.calls, 1)

    def test_retries_a_busy_upstream_then_succeeds(self):
        # 529 是真见过的 system_overloaded。它必须被重试，而不是当成坏请求丢掉。
        recorder = self.fake([http_error(529, "system_overloaded"), ANSWER])
        payload, _latency, _cached = server.ask_jev("state", "key")
        self.assertEqual(payload["model"], "jev-test")
        self.assertEqual(recorder.calls, 2)

    def test_retries_the_transient_statuses(self):
        for status in (408, 425, 429, 500, 502, 503, 504, 529):
            with self.subTest(status=status):
                recorder = self.fake([http_error(status), ANSWER])
                server.ask_jev("state", "key")
                self.assertEqual(recorder.calls, 2)

    def test_does_not_retry_a_bad_request(self):
        # 400/401 这类重试没有意义：同样的请求只会得到同样的拒绝。
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                recorder = self.fake([http_error(status)])
                with self.assertRaises(server.JevError):
                    server.ask_jev("state", "key")
                self.assertEqual(recorder.calls, 1)

    def test_gives_up_after_attempts_and_reports_the_reason(self):
        recorder = self.fake([http_error(503)] * server.ATTEMPTS)
        with self.assertRaises(server.JevError) as caught:
            server.ask_jev("state", "key")
        self.assertIn("503", str(caught.exception))
        self.assertEqual(recorder.calls, server.ATTEMPTS)

    def test_retries_network_errors_too(self):
        recorder = self.fake([urllib.error.URLError("no route"), ANSWER])
        server.ask_jev("state", "key")
        self.assertEqual(recorder.calls, 2)

    def test_every_timeout_stays_inside_the_budget(self):
        # 单个请求的超时不能超过总预算，否则「30 秒内一定有结果」就是假的。
        recorder = self.fake([http_error(503)] * server.ATTEMPTS)
        with self.assertRaises(server.JevError):
            server.ask_jev("state", "key", budget=12)
        for timeout in recorder.timeouts:
            self.assertLessEqual(timeout, 12)
            self.assertGreater(timeout, 0)

    def test_a_spent_budget_stops_immediately(self):
        recorder = self.fake([http_error(503)] * server.ATTEMPTS)
        with self.assertRaises(server.JevError) as caught:
            server.ask_jev("state", "key", budget=0)
        self.assertEqual(recorder.calls, 0)
        self.assertIn("预算", str(caught.exception))

    def test_the_error_never_leaks_the_api_key(self):
        self.fake([http_error(401, "bad key")])
        with self.assertRaises(server.JevError) as caught:
            server.ask_jev("state", "super-secret-key")
        self.assertNotIn("super-secret-key", str(caught.exception))


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.cache = server.Cache(self.path)
        self.original = urllib.request.urlopen
        os.environ.pop(server.CACHE_ENV, None)

    def tearDown(self):
        urllib.request.urlopen = self.original
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def test_round_trip(self):
        self.cache.put(b"body", ANSWER)
        self.assertEqual(self.cache.get(b"body")["model"], "jev-test")

    def test_misses_are_none(self):
        self.assertIsNone(self.cache.get(b"never seen"))

    def test_different_bodies_do_not_collide(self):
        self.cache.put(b"body-a", ANSWER)
        self.assertIsNone(self.cache.get(b"body-b"))

    def test_failures_are_never_stored(self):
        # 把一次超时缓存下来，就把「网络坏了一分钟」变成了永久的错误答案。
        for bad in ({}, {"error": "boom"}, {"answers": {}}, None, "nope"):
            self.cache.put(b"body", bad)
        self.assertIsNone(self.cache.get(b"body"))

    def test_a_broken_cache_file_degrades_to_a_miss(self):
        broken = server.Cache("/dev/null/definitely/not/a/file.db")
        self.assertIsNone(broken.get(b"body"))
        broken.put(b"body", ANSWER)      # 不能抛

    def test_a_second_call_is_served_from_cache_without_hitting_the_network(self):
        urllib.request.urlopen = UrlopenRecorder([ANSWER])
        server.CACHE = self.cache
        try:
            _payload, _latency, cached = server.ask_jev("state", "key")
            self.assertFalse(cached)
            _payload, _latency, cached = server.ask_jev("state", "key")
            self.assertTrue(cached)
        finally:
            server.CACHE = server.Cache(server.CACHE_PATH)


class HealthTest(unittest.TestCase):
    def test_api_key_reads_either_name(self):
        original = dict(os.environ)
        try:
            for name in ("JEV_API_KEY", "TYPESAFE_API_KEY"):
                for other in ("JEV_API_KEY", "TYPESAFE_API_KEY"):
                    os.environ.pop(other, None)
                os.environ[name] = "  spaced-key  "
                self.assertEqual(server.api_key(), "spaced-key")
            for name in ("JEV_API_KEY", "TYPESAFE_API_KEY"):
                os.environ.pop(name, None)
            self.assertIsNone(server.api_key())
        finally:
            os.environ.clear()
            os.environ.update(original)


class AsrTest(unittest.TestCase):
    def test_no_command_means_not_available(self):
        os.environ.pop(server.ASR_ENV, None)
        self.assertIsNone(server.asr_command())
        self.assertFalse(server.asr_available())

    def test_a_missing_binary_is_not_available(self):
        os.environ[server.ASR_ENV] = "definitely-not-a-real-binary-xyz {wav}"
        try:
            self.assertFalse(server.asr_available())
            with self.assertRaises(NotImplementedError):
                server.transcribe(b"RIFF")
        finally:
            os.environ.pop(server.ASR_ENV, None)

    def test_a_real_command_transcribes_and_gets_the_audio(self):
        # 用 cat 冒充识别器：它会把收到的文件原样打出来，正好证明 {wav} 真的指向音频。
        os.environ[server.ASR_ENV] = "cat {wav}"
        try:
            self.assertEqual(server.transcribe(b"ROCK-SCISSORS-PAPER"), "ROCK-SCISSORS-PAPER")
        finally:
            os.environ.pop(server.ASR_ENV, None)

    def test_the_extension_is_used_for_the_temp_file(self):
        # 有些识别工具按后缀猜容器格式，所以浏览器给的格式要传到文件名上。
        os.environ[server.ASR_ENV] = "sh -c 'basename {wav}'"
        try:
            self.assertTrue(server.transcribe(b"x", "webm").endswith(".webm"))
            self.assertTrue(server.transcribe(b"x", "wav").endswith(".wav"))
        finally:
            os.environ.pop(server.ASR_ENV, None)

    def test_a_hostile_extension_cannot_escape_the_temp_directory(self):
        os.environ[server.ASR_ENV] = "sh -c 'basename {wav}'"
        try:
            name = server.transcribe(b"x", "../../../etc/passwd")
            self.assertNotIn("/", name)
            self.assertNotIn("..", name)
        finally:
            os.environ.pop(server.ASR_ENV, None)

    def test_the_temp_file_is_cleaned_up(self):
        os.environ[server.ASR_ENV] = "sh -c 'basename {wav}'"
        try:
            name = server.transcribe(b"x", "wav").strip()
            self.assertFalse(os.path.exists(os.path.join(tempfile.gettempdir(), name)))
        finally:
            os.environ.pop(server.ASR_ENV, None)

    def test_a_failing_command_raises_with_stderr(self):
        os.environ[server.ASR_ENV] = "sh -c 'echo bad-thing >&2; exit 3'"
        try:
            with self.assertRaises(RuntimeError) as caught:
                server.transcribe(b"x")
            self.assertIn("bad-thing", str(caught.exception))
        finally:
            os.environ.pop(server.ASR_ENV, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
