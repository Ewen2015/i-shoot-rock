#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""game.py 的单元测试。只跑纯逻辑，不需要 API key，也不联网。

    python3 test_game.py
"""

from __future__ import annotations

import random
import unittest

import game


class OutcomeTest(unittest.TestCase):
    def test_beat_relations(self):
        self.assertEqual(game.outcome("rock", "scissors"), "user")
        self.assertEqual(game.outcome("scissors", "paper"), "user")
        self.assertEqual(game.outcome("paper", "rock"), "user")

    def test_losing_and_tying(self):
        self.assertEqual(game.outcome("scissors", "rock"), "jev")
        self.assertEqual(game.outcome("rock", "paper"), "jev")
        self.assertEqual(game.outcome("paper", "scissors"), "jev")
        self.assertEqual(game.outcome("paper", "paper"), "tie")

    def test_every_pair_is_covered_exactly_once(self):
        seen = set()
        for user in game.THROWS:
            for jev in game.THROWS:
                seen.add((user, jev, game.outcome(user, jev)))
        self.assertEqual(len(seen), 9)

    def test_rejects_unknown_throw(self):
        with self.assertRaises(ValueError):
            game.outcome("lizard", "rock")


class BeatsTableTest(unittest.TestCase):
    def test_beats_and_loses_to_are_inverses(self):
        for throw in game.THROWS:
            self.assertEqual(game.LOSES_TO[game.BEATS[throw]], throw)
            self.assertNotEqual(game.BEATS[throw], throw)

    def test_the_table_is_a_three_cycle_over_exactly_our_throws(self):
        self.assertEqual(set(game.BEATS), set(game.THROWS))
        self.assertEqual(set(game.BEATS.values()), set(game.THROWS))


class BuildStateTest(unittest.TestCase):
    def test_empty_history_says_round_one(self):
        state = game.build_state([])
        self.assertIn("第 1 回合", state)
        self.assertIn("石头胜剪刀", state)

    def test_history_is_written_out_with_both_throws_and_the_result(self):
        state = game.build_state([("rock", "scissors", "user"), ("paper", "paper", "tie")])
        self.assertIn("共 2 回合", state)
        self.assertIn("第 1 回合：用户出石头，你出剪刀", state)
        self.assertIn("你赢了", state)
        self.assertIn("第 2 回合：用户出布，你出布", state)
        self.assertIn("平局", state)

    def test_state_is_second_person_so_jev_owns_the_right_side(self):
        # 「你出剪刀」里的「你」必须是 Jev；写反了它会把人机立场搞混。
        state = game.build_state([("rock", "scissors", "user")])
        self.assertIn("用户出石头", state)
        self.assertIn("你出剪刀", state)


class NormalizeProbsTest(unittest.TestCase):
    def test_passthrough_when_already_a_distribution(self):
        probs = game.normalize_probs({"rock": 0.5, "paper": 0.3, "scissors": 0.2})
        self.assertAlmostEqual(probs["rock"], 0.5)
        self.assertAlmostEqual(sum(probs.values()), 1.0)

    def test_missing_keys_become_zero(self):
        probs = game.normalize_probs({"rock": 1.0})
        self.assertAlmostEqual(probs["rock"], 1.0)
        self.assertAlmostEqual(probs["paper"], 0.0)

    def test_unnormalized_input_is_normalized(self):
        probs = game.normalize_probs({"rock": 3, "paper": 1, "scissors": 0})
        self.assertAlmostEqual(probs["rock"], 0.75)

    def test_empty_or_garbage_falls_back_to_uniform(self):
        for bad in ({}, None, {"rock": "oops"}, {"rock": -1, "paper": -2}):
            probs = game.normalize_probs(bad or {})
            for throw in game.THROWS:
                self.assertAlmostEqual(probs[throw], 1.0 / 3.0)


class ExpectedValuesTest(unittest.TestCase):
    def test_certain_rock_makes_paper_the_best_play(self):
        ev = game.expected_values({"rock": 1.0, "paper": 0.0, "scissors": 0.0})
        self.assertAlmostEqual(ev["paper"], 1.0)
        self.assertAlmostEqual(ev["rock"], 0.0)
        self.assertAlmostEqual(ev["scissors"], -1.0)

    def test_values_always_sum_to_zero(self):
        # 三种出拳互相抵消：有人赚 1 就有人亏 1。
        for probs in (
            {"rock": 0.5, "paper": 0.3, "scissors": 0.2},
            {"rock": 0.9, "paper": 0.05, "scissors": 0.05},
            {"rock": 1 / 3, "paper": 1 / 3, "scissors": 1 / 3},
        ):
            ev = game.expected_values(probs)
            self.assertAlmostEqual(sum(ev.values()), 0.0, places=9)

    def test_uniform_belief_is_indifference(self):
        ev = game.expected_values({"rock": 1 / 3, "paper": 1 / 3, "scissors": 1 / 3})
        for throw in game.THROWS:
            self.assertAlmostEqual(ev[throw], 0.0, places=9)


class ChooseThrowTest(unittest.TestCase):
    def test_always_counters_a_confident_belief(self):
        # 核心承诺：Jev 越确定人会出什么，选中的那一手就越是能赢它。
        for expected in game.THROWS:
            probs = {throw: 0.0 for throw in game.THROWS}
            probs[expected] = 1.0
            self.assertEqual(game.choose_throw(probs), game.LOSES_TO[expected])

    def test_certainty_never_loses(self):
        for expected in game.THROWS:
            probs = {throw: 0.0 for throw in game.THROWS}
            probs[expected] = 1.0
            played = game.choose_throw(probs)
            self.assertNotEqual(game.outcome(played, expected), "jev")

    def test_rock_paper_50_50_plays_paper_not_rock(self):
        # 反例，写下来免得以后「优化」错：石头 50% 时该出布（+0.5），
        # 而不是跟着出石头（-0.5）。跟随后验的最高频出拳是错的，要出能赢它的那一手。
        probs = {"rock": 0.5, "paper": 0.5, "scissors": 0.0}
        self.assertEqual(game.choose_throw(probs), "paper")

    def test_is_deterministic_and_always_picks_the_argmax(self):
        # 精确的「并列最高」在浮点下几乎不可达，所以不写死一个并列的例子，改测性质：
        # 同一份信念永远给同一手，且那一手确实是期望值最高的。
        rng = random.Random(20260928)
        for _ in range(300):
            probs = {throw: rng.random() for throw in game.THROWS}
            chosen = game.choose_throw(probs)
            self.assertEqual(chosen, game.choose_throw(probs))
            ev = game.expected_values(probs)
            self.assertAlmostEqual(ev[chosen], max(ev.values()), places=12)

    def test_uniform_belief_falls_back_to_rock_deterministically(self):
        # Jev 完全没倾向时三边都是 0，固定给石头：不是猜对了，是没有信息。
        uniform = {"rock": 1 / 3, "paper": 1 / 3, "scissors": 1 / 3}
        self.assertEqual(game.choose_throw(uniform), "rock")
        self.assertEqual(game.choose_throw(uniform), game.choose_throw(uniform))

    def test_decide_returns_probabilities_expected_and_throw(self):
        decided = game.decide({"rock": 0.8, "paper": 0.1, "scissors": 0.1})
        self.assertEqual(decided["throw"], "paper")
        self.assertAlmostEqual(sum(decided["probabilities"].values()), 1.0)
        self.assertIn("paper", decided["expected"])


class ParseUtteranceTest(unittest.TestCase):
    """语音这一段最容易出错：「石头剪刀布」里含着三种出拳的名字。"""

    def test_chant_alone_is_not_a_throw(self):
        # 最要紧的一条：「石头剪刀布」是口诀，不是出了石头。
        self.assertIsNone(game.parse_utterance("石头剪刀布")["throw"])

    def test_throw_after_the_chant(self):
        for text, expect in (
            ("石头剪刀布，剪刀", "scissors"),
            ("石头剪刀布！石头", "rock"),
            ("石头剪刀布 布", "paper"),
            ("石头剪刀布，我出布", "paper"),
        ):
            self.assertEqual(game.parse_utterance(text)["throw"], expect, text)

    def test_bare_throw_without_a_chant(self):
        self.assertEqual(game.parse_utterance("石头")["throw"], "rock")
        self.assertEqual(game.parse_utterance("剪刀")["throw"], "scissors")
        self.assertEqual(game.parse_utterance("布")["throw"], "paper")

    def test_last_throw_wins_so_people_can_correct_themselves(self):
        self.assertEqual(game.parse_utterance("石头，不对，布")["throw"], "paper")

    def test_variant_words(self):
        self.assertEqual(game.parse_utterance("剪子")["throw"], "scissors")
        self.assertEqual(game.parse_utterance("锤子")["throw"], "rock")
        self.assertEqual(game.parse_utterance("包袱")["throw"], "paper")

    def test_english_works_for_testing(self):
        self.assertEqual(game.parse_utterance("rock paper scissors, scissors")["throw"], "scissors")

    def test_punctuation_and_case_are_ignored(self):
        self.assertEqual(game.parse_utterance("  Rock Paper Scissors ，  PAPER ！ ")["throw"], "paper")

    def test_start_command(self):
        self.assertEqual(game.parse_utterance("开始")["command"], "start")
        self.assertEqual(game.parse_utterance("开始吧")["command"], "start")

    def test_new_session_wins_over_start_when_both_appear(self):
        self.assertEqual(game.parse_utterance("再来一个回合，开始")["command"], "new_session")

    def test_new_session_phrasings(self):
        for text in ("再来一个回合", "再来一局", "重来", "换一个回合"):
            self.assertEqual(game.parse_utterance(text)["command"], "new_session", text)

    def test_command_and_throw_can_arrive_together(self):
        parsed = game.parse_utterance("开始，石头剪刀布，剪刀")
        self.assertEqual(parsed["command"], "start")
        self.assertEqual(parsed["throw"], "scissors")

    def test_silence_and_noise_yield_nothing(self):
        for text in ("", "   ", "……", "嗯", None):
            parsed = game.parse_utterance(text)
            self.assertIsNone(parsed["command"])
            self.assertIsNone(parsed["throw"])

    def test_a_throw_word_inside_a_longer_sentence(self):
        self.assertEqual(game.parse_utterance("我觉得我这次要出剪刀了")["throw"], "scissors")


class SoundFallbackTest(unittest.TestCase):
    """识别器是按音写字的：它把「布」写成「不」是常态。

    所以出拳不能只看字面，要先把字还原成音再比。这一组测的就是这条兜底。
    """

    def test_negative_adverb_is_paper(self):
        # 用户实际遇到的那一条：喊「布」，识别成「不」。
        self.assertEqual(game.parse_utterance("不")["throw"], "paper")
        self.assertEqual(game.parse_utterance("石头剪刀布，不")["throw"], "paper")

    def test_every_common_homophone_of_bu(self):
        for char in "布不步部埠怖补":
            self.assertEqual(game.parse_utterance("石头剪刀布，" + char)["throw"], "paper", char)

    def test_homophones_of_the_other_throws(self):
        for text in ("时头", "十头", "石头"):
            self.assertEqual(game.parse_utterance("石头剪刀布，" + text)["throw"], "rock", text)
        for text in ("尖刀", "剑刀", "剪刀"):
            self.assertEqual(game.parse_utterance("石头剪刀布，" + text)["throw"], "scissors", text)
        for text in ("包伏", "包服", "包袱"):
            self.assertEqual(game.parse_utterance("石头剪刀布，" + text)["throw"], "paper", text)

    def test_near_sounds_are_accepted(self):
        # 翘舌/平舌、前后鼻音，是识别器最常混的两组。
        self.assertEqual(game.parse_utterance("石头剪刀布，全头")["throw"], "rock")
        self.assertEqual(game.parse_utterance("石头剪刀布，吹子")["throw"], "rock")
        self.assertEqual(game.parse_utterance("石头剪刀布，剪子")["throw"], "scissors")

    def test_a_negative_phrase_is_not_a_throw(self):
        for text in ("不对", "不知道", "不要", "不行", "不是"):
            self.assertIsNone(game.parse_utterance(text)["throw"], text)

    def test_the_correction_still_lands_on_the_last_throw(self):
        # 「不对」里的那个「不」不能被当成布，把用户真正的出拳盖掉。
        self.assertEqual(game.parse_utterance("石头，不对，布")["throw"], "paper")
        self.assertEqual(game.parse_utterance("石头，不对，剪刀")["throw"], "scissors")

    def test_a_mangled_chant_is_still_only_a_chant(self):
        # 口诀末尾的「布」被写成「不」时，整句仍是口诀，不是出了布（也不是剪刀）。
        self.assertIsNone(game.parse_utterance("石头剪刀不")["throw"])
        self.assertIsNone(game.parse_utterance("石头剪子不")["throw"])

    def test_sound_layer_covers_every_word_we_accept(self):
        # 表里漏掉一个字，那一手就会悄悄退回字面匹配。这条测试是给表做体检的。
        for chant in game.CHANTS:
            self.assertIsNotNone(game.sound_keys(chant), chant)
        for throw, words in game.THROW_WORDS:
            for word in words:
                if word.isascii():
                    continue
                self.assertIsNotNone(game.sound_keys(word), word)


class HistoryTest(unittest.TestCase):
    def test_round_record_computes_the_result(self):
        self.assertEqual(game.round_record("rock", "scissors")["result"], "user")
        self.assertEqual(game.round_record("rock", "paper")["result"], "jev")
        self.assertEqual(game.round_record("rock", "rock")["result"], "tie")

    def test_history_from_records_skips_junk(self):
        records = [
            {"user": "rock", "jev": "scissors"},
            {"user": "nonsense", "jev": "rock"},
            {},
            None,
            {"user": "paper", "jev": "paper"},
        ]
        self.assertEqual(
            game.history_from_records(records),
            [("rock", "scissors", "user"), ("paper", "paper", "tie")],
        )

    def test_history_from_records_handles_none(self):
        self.assertEqual(game.history_from_records(None), [])

    def test_tally(self):
        records = [
            {"user": "rock", "jev": "scissors"},
            {"user": "rock", "jev": "paper"},
            {"user": "paper", "jev": "paper"},
            {"user": "scissors", "jev": "paper"},
        ]
        c = game.tally(records)
        self.assertEqual((c["user"], c["jev"], c["tie"], c["played"]), (2, 1, 1, 4))
        self.assertAlmostEqual(c["user_rate"], 0.5)

    def test_tally_of_nothing_is_all_zero(self):
        c = game.tally([])
        self.assertEqual(c["played"], 0)
        self.assertEqual(c["user_rate"], 0.0)


class EndToEndLogicTest(unittest.TestCase):
    def test_history_accumulates_into_the_state(self):
        history = []
        states = [game.build_state(history)]
        for _ in range(4):
            history.append(game.round_record("rock", "paper"))
            states.append(game.build_state(game.history_from_records(history)))
        self.assertIn("第 1 回合", states[0])
        self.assertNotIn("第 2 回合", states[1])
        self.assertIn("第 4 回合", states[4])
        self.assertIn("共 4 回合", states[4])

    def test_a_cleared_history_reads_as_a_fresh_session(self):
        full = game.history_from_records([{"user": "rock", "jev": "paper"}] * 3)
        self.assertIn("共 3 回合", game.build_state(full))
        self.assertIn("第 1 回合", game.build_state([]))
        self.assertNotIn("第 1 回合：", game.build_state([]))


class DecideFromAnswersTest(unittest.TestCase):
    def test_uses_only_the_prediction_to_pick_the_throw(self):
        answers = {
            "prediction": {"choice": "rock", "probabilities": {"rock": 0.9, "paper": 0.05, "scissors": 0.05}},
            # 直接问它的那一手故意给一个会输的，确保它不参与决策。
            "direct": {"choice": "scissors"},
        }
        decided = game.decide_from_answers(answers)
        self.assertEqual(decided["throw"], "paper")
        self.assertEqual(decided["direct"], "scissors")
        self.assertFalse(decided["agrees"])

    def test_reports_agreement_when_jev_picks_the_same_throw(self):
        answers = {
            "prediction": {"probabilities": {"rock": 0.9, "paper": 0.05, "scissors": 0.05}},
            "direct": {"choice": "paper"},
        }
        self.assertTrue(game.decide_from_answers(answers)["agrees"])

    def test_survives_a_missing_or_broken_direct_answer(self):
        for answers in ({}, None, {"prediction": {"probabilities": {}}}, {"direct": {"choice": "lizard"}}):
            decided = game.decide_from_answers(answers)
            self.assertIn(decided["throw"], game.THROWS)
            self.assertIsNone(decided["direct"])
            self.assertIsNone(decided["agrees"])

    def test_survives_a_broken_prediction(self):
        decided = game.decide_from_answers({"prediction": {"probabilities": {"rock": "x"}}})
        self.assertEqual(decided["throw"], "rock")
        self.assertAlmostEqual(sum(decided["probabilities"].values()), 1.0)


class QuestionTest(unittest.TestCase):
    def test_asks_for_a_belief_about_the_user_and_nothing_else_decides(self):
        q = game.question()
        self.assertIn("prediction", q)
        self.assertEqual(q["prediction"]["type"], "choice")
        self.assertEqual(set(q["prediction"]["criteria"]), set(game.THROWS))

    def test_direct_question_is_present_but_separate(self):
        self.assertIn("direct", game.question())


if __name__ == "__main__":
    unittest.main(verbosity=2)
