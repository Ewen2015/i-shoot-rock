#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""settings.py 的测试：优先级、落盘的权限，以及「key 永远不回浏览器」这一条。

全部在临时目录里跑，不碰真的密钥文件，也不联网。

    python3 tests/test_settings.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import settings


class SettingsTestCase(unittest.TestCase):
    """每个用例一个空目录，环境变量退回到「什么都没设」的样子。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "settings.json")
        self.saved = {name: os.environ.get(name) for name in
                      ("JEV_API_KEY", "TYPESAFE_API_KEY", settings.PATH_ENV)}
        os.environ[settings.PATH_ENV] = self.path
        for name in settings.ENV_NAMES["jev_api_key"]:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class FileTest(SettingsTestCase):
    def test_a_saved_key_comes_back(self):
        settings.save({"jev_api_key": "sk-abcdefghijklmnop"})
        self.assertEqual(settings.get("jev_api_key"), "sk-abcdefghijklmnop")
        self.assertEqual(settings.key_value("jev_api_key"), "sk-abcdefghijklmnop")
        self.assertEqual(settings.key_source("jev_api_key"), "settings")

    def test_the_file_is_private(self):
        # 0600：这是一个秘密，它不该被同机器上的别人读到。
        settings.save({"jev_api_key": "sk-abcdefghijklmnop"})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_an_empty_value_clears_the_field(self):
        settings.save({"jev_api_key": "sk-abcdefghijklmnop"})
        settings.save({"jev_api_key": ""})
        self.assertIsNone(settings.get("jev_api_key"))
        # 没有别的字段了，文件本身也不该留在那儿装样子。
        self.assertFalse(os.path.exists(self.path))

    def test_surrounding_whitespace_is_trimmed(self):
        settings.save({"jev_api_key": "  sk-abcdefghijklmnop\n"})
        self.assertEqual(settings.get("jev_api_key"), "sk-abcdefghijklmnop")

    def test_the_directory_is_created_on_demand(self):
        # var/ 是运行时才出现的目录，第一次保存不能因此失败。
        os.environ[settings.PATH_ENV] = os.path.join(self.dir.name, "var", "settings.json")
        settings.save({"jev_api_key": "sk-abcdefghijklmnop"})
        self.assertTrue(os.path.isfile(os.environ[settings.PATH_ENV]))

    def test_no_temp_file_is_left_behind(self):
        settings.save({"jev_api_key": "sk-abcdefghijklmnop"})
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_a_corrupt_file_reads_as_no_keys(self):
        # 坏文件等于没有文件：一个坏掉的设置文件不能让服务器起不来。
        for junk in ("{ not json", "[]", '"a string"', ""):
            with self.subTest(junk=junk):
                with open(self.path, "w", encoding="utf-8") as handle:
                    handle.write(junk)
                self.assertEqual(settings.load(), {})
                self.assertIsNone(settings.key_value("jev_api_key"))

    def test_a_non_string_value_is_ignored(self):
        # 手改过或坏了的文件里可能有 123。把它变成 "123" 只会把事故推到很久以后，
        # 以一次 401 的样子出现。
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"jev_api_key": 123}, handle)
        self.assertEqual(settings.load(), {})

    def test_unknown_fields_are_never_stored(self):
        settings.save({"deepseek_api_key": "sk-nope"})
        self.assertFalse(os.path.exists(self.path))


class PrecedenceTest(SettingsTestCase):
    def test_the_environment_is_used_when_there_is_no_file(self):
        os.environ["TYPESAFE_API_KEY"] = "  from-env  "
        self.assertEqual(settings.key_value("jev_api_key"), "from-env")
        self.assertEqual(settings.key_source("jev_api_key"), "env")

    def test_the_file_wins_over_the_environment(self):
        # 在页面上按保存是一个看得见结果的主动动作；让它输给某个忘掉的 export，
        # 是那种要花一小时才查出来的事。
        os.environ["JEV_API_KEY"] = "from-env"
        settings.save({"jev_api_key": "from-file"})
        self.assertEqual(settings.key_value("jev_api_key"), "from-file")
        self.assertEqual(settings.key_source("jev_api_key"), "settings")

    def test_either_environment_name_is_read(self):
        for name in ("JEV_API_KEY", "TYPESAFE_API_KEY"):
            with self.subTest(name=name):
                for other in ("JEV_API_KEY", "TYPESAFE_API_KEY"):
                    os.environ.pop(other, None)
                os.environ[name] = "from-env"
                self.assertEqual(settings.key_value("jev_api_key"), "from-env")

    def test_nothing_configured_is_none(self):
        self.assertIsNone(settings.key_value("jev_api_key"))
        self.assertIsNone(settings.key_source("jev_api_key"))


class MaskTest(unittest.TestCase):
    def test_four_and_four_are_enough_to_tell_two_keys_apart(self):
        self.assertEqual(settings.mask("sk-abcdefghijklmnop"), "sk-a…mnop")

    def test_a_short_key_gives_nothing_away(self):
        self.assertEqual(settings.mask("short"), "…")
        self.assertIsNone(settings.mask(""))
        self.assertIsNone(settings.mask(None))


class StatusTest(SettingsTestCase):
    def test_the_status_never_carries_the_key(self):
        # 这是整个「设置」窗口存在的理由：它可以显示「填了没有」，但 key 本身
        # 一次都不许出现在 HTTP 响应里。
        secret = "sk-abcdefghijklmnop"
        settings.save({"jev_api_key": secret})
        report = settings.key_status()
        self.assertNotIn(secret, json.dumps(report, ensure_ascii=False))
        self.assertEqual(report["jev_api_key"]["hint"], "sk-a…mnop")
        self.assertTrue(report["jev_api_key"]["configured"])
        self.assertEqual(report["file"], self.path)
        self.assertTrue(report["file_exists"])

    def test_it_says_where_the_live_key_comes_from(self):
        os.environ["JEV_API_KEY"] = "from-env"
        self.assertEqual(settings.key_status()["jev_api_key"]["source"], "env")
        settings.save({"jev_api_key": "from-file"})
        self.assertEqual(settings.key_status()["jev_api_key"]["source"], "settings")
        settings.save({"jev_api_key": ""})
        os.environ.pop("JEV_API_KEY")
        self.assertFalse(settings.key_status()["jev_api_key"]["configured"])

    def test_the_environment_hint_names_both_variables(self):
        self.assertIn("JEV_API_KEY", settings.key_status()["jev_api_key"]["env_hint"])


class ValidateTest(unittest.TestCase):
    def test_a_clean_key_is_returned_trimmed(self):
        self.assertEqual(settings.validate("jev_api_key", "  sk-abc  "), "sk-abc")

    def test_an_empty_value_means_clear(self):
        for value in ("", "   ", None):
            self.assertEqual(settings.validate("jev_api_key", value), "")

    def test_whitespace_in_the_middle_is_a_paste_accident(self):
        # 这个报错要指着那个真的原因：换行和空格几乎总是粘贴带进来的，
        # 而一个 401 从来不告诉人这件事。
        for value in ("sk-abc\ndef", "sk abc", "sk\tabc"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    settings.validate("jev_api_key", value)
                self.assertIn("空白字符", str(caught.exception))

    def test_an_absurdly_long_value_is_refused(self):
        with self.assertRaises(ValueError):
            settings.validate("jev_api_key", "x" * (settings.MAX_KEY_LENGTH + 1))

    def test_a_non_string_is_refused(self):
        with self.assertRaises(ValueError):
            settings.validate("jev_api_key", 12345)

    def test_a_patch_ignores_names_this_file_does_not_hold(self):
        # 一个什么名字都收的接口，等于邀请一次拼错。
        self.assertEqual(settings.patch_from({"deepseek_api_key": "x"}), {})
        self.assertEqual(settings.patch_from({"jev_api_key": "sk-abc"}), {"jev_api_key": "sk-abc"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
