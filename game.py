#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""石头剪刀布 · 对 Jev —— 纯逻辑层。

这个文件里没有网络、没有磁盘、没有时钟：全是入参到出参的函数。它装着三样东西，
也就是这场游戏里唯一值得单独测的部分：

  1. 猜拳规则本身（谁赢谁、三种结果）
  2. 交给 Jev 的 `state` 原文（把历史轮次和输赢写进它的 memory）
  3. 把 Jev 的信念变成一次出拳的算术

第 3 点是这个游戏最要紧的设计，值得说清楚：实测下来，直接问 Jev「你出什么」
并不可靠 —— 告诉它「人连续出了三次石头」，它有时候会出剪刀（而剪刀是输给石头的），
同时在另一个问题里把「人会出石头」的概率给到 0.98。它很会判断人，但不擅长把判断
换算成一步好棋。所以这里的做法是：让 Jev 只负责它擅长的部分（说出它对下一手的
信念分布），出拳由 `choose_throw` 在规则之内算出来 —— 挑期望胜率最高的那个。
出拳仍然完全由 Jev 决定，只是它决定的方式是给概率而不是给动作。
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# 规则
# --------------------------------------------------------------------------- #

THROWS = ("rock", "paper", "scissors")

LABEL = {"rock": "石头", "paper": "布", "scissors": "剪刀"}
LABEL_TO_THROW = {label: throw for throw, label in LABEL.items()}

# BEATS[a] 是被 a 打败的那一手。
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
# LOSES_TO[a] 是能打败 a 的那一手，也就是 a 的天敌。
LOSES_TO = {beaten: winner for winner, beaten in BEATS.items()}


def outcome(user, jev):
    """这一回合谁赢了：'user'、'jev' 或 'tie'。"""
    if user not in BEATS or jev not in BEATS:
        raise ValueError("unknown throw: %r / %r" % (user, jev))
    if user == jev:
        return "tie"
    return "user" if BEATS[user] == jev else "jev"


OUTCOME_LABEL = {"user": "你赢了", "jev": "Jev 赢了", "tie": "平局"}


# --------------------------------------------------------------------------- #
# 交给 Jev 的 state
# --------------------------------------------------------------------------- #

RULES_ZH = (
    "石头剪刀布的规则：石头胜剪刀，剪刀胜布，布胜石头。出拳能赢过对方时本回合获胜，"
    "两人出拳相同则平局。\n"
    "你和一个人每回合同时出一次拳，你必须在自己看到对方出拳之前先决定出拳。"
    "你的目标是尽可能多赢几局。\n"
)


def build_state(history):
    """把已完成的轮次写成 Jev 的 `state`。

    `history` 是 (用户出拳, Jev 出拳, 结果) 的序列。历史为空时明确告诉它这是第一
    回合，而不是留一段空白的「历史：」——后者会让它以为信息缺失。
    """
    if not history:
        body = "目前还没有进行过任何回合。这将是第 1 回合。"
    else:
        lines = [
            "第 %d 回合：用户出%s，你出%s，结果：%s。"
            % (i, LABEL[user], LABEL[jev], describe(user, jev, result))
            for i, (user, jev, result) in enumerate(history, 1)
        ]
        body = "已完成的回合（共 %d 回合）：\n%s" % (len(history), "\n".join(lines))
    return RULES_ZH + body


def describe(user, jev, result):
    """轮次里那句「结果：……」。写全，而不是只写「用户赢了」。

    Jev 拿到的是自然语言，把「谁赢」和「怎么赢的」都写出来，比让它自己从两次
    出拳里推要省事，也让 state 更像一段给人看的记录。
    """
    if result == "tie":
        return "平局（双方都出%s）" % LABEL[user]
    winner, loser = (user, jev) if result == "user" else (jev, user)
    return "%s（%s 胜 %s）" % (OUTCOME_LABEL[result], LABEL[winner], LABEL[loser])


def question():
    """一次问 Jev 两个问题。

    `prediction` 是真正决定出拳的那个：它对用户下一手的信念分布。`direct` 是同一
    次调用里顺带问的「你自己出什么」，只用来说明，不参与决策 —— 实测它并不可靠
    （告诉它人连出三次石头，它有相当一部分时候会出剪刀，也就是输给石头的那一手），
    所以把它放在 UI 上当旁注，让人看见模型真实的样子，而不是拿它下棋。

    两个问题在同一次 HTTP 调用里，所以旁注不额外花一次往返。
    """
    return {
        "prediction": {
            "type": "choice",
            "instructions": "在即将到来的这一回合里，用户最可能出什么？",
            "criteria": {
                "rock": "用户出石头",
                "paper": "用户出布",
                "scissors": "用户出剪刀",
            },
        },
        "direct": {
            "type": "choice",
            "instructions": "如果这一回合要你自己出拳，你会出什么？",
            "criteria": {
                "rock": "你出石头",
                "paper": "你出布",
                "scissors": "你出剪刀",
            },
        },
    }


# --------------------------------------------------------------------------- #
# 从信念到出拳
# --------------------------------------------------------------------------- #


def normalize_probs(probs):
    """三种出拳的概率，缺的补 0，负数当 0，再归一化。

    Jev 给的概率实测都在 0..1 且和为 1，但这是在拿它当算术输入用，所以对残缺的
    回答要有一个明确的降级方式，而不是让 KeyError 冒到游戏循环里。
    """
    clean = {}
    for throw in THROWS:
        try:
            value = float(probs.get(throw, 0.0))
        except (TypeError, ValueError):
            value = 0.0
        clean[throw] = value if value > 0.0 else 0.0
    total = sum(clean.values())
    if total <= 0.0:
        return {throw: 1.0 / len(THROWS) for throw in THROWS}
    return {throw: value / total for throw, value in clean.items()}


def expected_values(probs):
    """每一手出拳的期望净胜分：赢的概率减去输的概率。

    平局记为 0，所以这个数的范围是 -1..1，0 表示「不赚不亏」。用净胜分而不是
    「赢的概率」是因为两者在这里等价（每种出拳赢的概率 + 输的概率 + 平局概率
    = 1，平局概率对该手是定值），但净胜分可以直接比较大小，也能直接说给人听。
    """
    p = normalize_probs(probs)
    return {
        "rock": p["scissors"] - p["paper"],
        "paper": p["rock"] - p["scissors"],
        "scissors": p["paper"] - p["rock"],
    }


def choose_throw(probs):
    """挑期望净胜分最高的一手。平手时按 THROWS 的顺序取靠前的那个，保证确定性。

    当 Jev 完全没倾向（三种各 1/3）时三边的净胜分都是 0，这里会返回 `石头` ——
    这不是「猜对了」，只是「没有信息时固定的一手」，UI 应该照实说。
    """
    ev = expected_values(probs)
    return max(THROWS, key=lambda throw: (ev[throw], -THROWS.index(throw)))


def decide(probs):
    """从一份信念分布算出归一化概率、每手期望值、以及选中的那一手。"""
    clean = normalize_probs(probs)
    ev = expected_values(clean)
    return {"probabilities": clean, "expected": ev, "throw": choose_throw(clean)}


def decide_from_answers(answers):
    """把 Jev 那一次调用的原始 `answers` 变成游戏的出拳。

    只有 `prediction` 参与决策；`direct` 原样带出去给人看，不参与任何计算。
    """
    answers = answers or {}
    prediction = answers.get("prediction") or {}
    decided = decide(prediction.get("probabilities") or {})

    direct = (answers.get("direct") or {}).get("choice")
    if direct not in THROWS:
        direct = None

    decided.update({
        # Jev 自己直接选的那一手，以及它是否正好和算出来的那一手一致。
        "direct": direct,
        "agrees": (direct == decided["throw"]) if direct else None,
        "confidence": prediction.get("confidence"),
    })
    return decided


# --------------------------------------------------------------------------- #
# 语音：把一句话变成「指令」和「出拳」
# --------------------------------------------------------------------------- #

# 口诀。用户说出它之后紧接着那一手才是真正的出拳，所以要先把口诀剥掉 ——
# 「石头剪刀布」这五个字里正好含着三种出拳的名字，不剥就会把它当成三次出拳。
CHANTS = ("石头剪刀布", "剪刀石头布", "布剪刀石头", "石头剪子布", "剪子石头布")

# 出拳的词。长的放前面，先匹配长的（「剪刀」不该被当成单个「刀」）。
THROW_WORDS = (
    ("scissors", ("剪刀", "剪子", "剪", "scissors")),
    ("rock", ("石头", "锤子", "锤", "拳", "rock")),
    ("paper", ("布", "包袱", "paper")),
)

# 指令词。按「越具体越先匹配」排：先认「再来一个回合」，再认更短的「再来」。
NEW_SESSION_WORDS = ("再来一个回合", "再来一回合", "再来一局", "换一个回合", "重来", "再来", "换一局")
START_WORDS = ("开始", "预备开始", "start")

# 语音识别会插标点和空格，比对前统一去掉。
DROP_CHARS = " \t\r\n，。！？,.!?、；;：:'\"“”‘’"


def normalize_text(text):
    """去掉标点空白并转小写，方便做包含判断。"""
    lowered = (text or "").lower()
    return "".join(ch for ch in lowered if ch not in DROP_CHARS)


def strip_chants(text):
    """把口诀从文本里删掉，返回剩下的部分。"""
    out = text
    for chant in CHANTS:
        out = out.replace(chant, " ")
    return out


# --------------------------------------------------------------------------- #
# 读音：识别器按音写字，所以按音认拳
# --------------------------------------------------------------------------- #
#
# 为什么需要这一层：语音识别把「布」写成「不」是常态 —— 同一个音，它随手挑一个
# 同音字，而且每次挑的还不一样。只按字面比对，人明明喊了「布」，解析出来却是空的。
# 人喊的是音，那就把字还原成音再比。
#
# 表里只收这个游戏用得上的音节（十来组），不引第三方拼音库：这个项目只用标准库。
# 收不进表的字不参与读音比对，只由下面字面那一层负责。

SYLLABLE_CHARS = {
    # jiǎn dāo / jiǎn zi
    "jian": "剪剑建见间简件健检减键兼捡箭监坚尖肩艰贱践溅茧柬碱荐奸煎歼鉴",
    "dao": "刀到道倒岛盗稻导悼蹈捣祷",
    "zi": "子字自资紫姿咨滋兹姊仔渍",
    # shí tou / chuí zi / quán
    "shi": "石时十实识是事师施失市式世室试视势释饰氏拾食蚀史使始驶矢屎虱湿诗狮尸",
    "tou": "头投偷透骰",
    "chui": "锤垂吹炊捶槌棰",
    "quan": "拳全权泉圈犬劝券痊蜷颧",
    # bù / bāo fu
    "bu": "布不步部埠怖捕补卜簿哺",
    "bao": "包报保抱暴宝饱胞堡苞褒鲍剥雹豹",
    "fu": "袱服伏福副富复付附负夫府幅符妇扶抚腹覆缚甫辅腐赴赋敷辐俯斧",
}

CHAR_SOUND = {char: syllable for syllable, chars in SYLLABLE_CHARS.items() for char in chars}

# 识别器最容易混的声母和韵母（翘舌/平舌、前后鼻音）。收敛到同一个键再比，
# 省得为一个音把每种口音的字都列一遍。
SOUND_FIXES = (
    ("zh", "z"), ("ch", "c"), ("sh", "s"),
    ("ang", "an"), ("eng", "en"), ("ing", "in"), ("ong", "on"),
)

# 「不」既是布，也是否定词。紧跟着这些字出现时，它是「不对」「不要」的那个不。
AMBIGUOUS_BU = "不卜"
NEGATIVE_TAILS = "对是要用行知好错会能想敢为如过但只光管论问妨怕准许同价值得免"


def canon_syllable(syllable):
    """把一个音节收敛成它的「音类」，让近音字也能对上。"""
    canonical = syllable
    for old, new in SOUND_FIXES:
        canonical = canonical.replace(old, new)
    return canonical


def sound_keys(text):
    """整句话的读音。有一个字收不进表就返回 None —— 这条读音不完整，不能当证据。"""
    keys = []
    for char in text:
        syllable = CHAR_SOUND.get(char)
        if syllable is None:
            return None
        keys.append(canon_syllable(syllable))
    return tuple(keys)


# 口诀的读音。口诀里也含着三种出拳的名字，不整段抠掉就会被数成一堆出拳。
CHANT_KEYS = tuple(key for key in (sound_keys(chant) for chant in CHANTS) if key)

# 出拳的读音。长的排前面，同一处命中多个时取更长的那个。
THROW_SOUNDS = (
    ("scissors", (("jian", "dao"), ("jian", "zi"), ("jian",))),
    ("rock", (("shi", "tou"), ("chui", "zi"), ("chui",), ("quan", "tou"), ("quan",))),
    ("paper", (("bu",), ("bao", "fu"))),
)
THROW_KEYS = tuple(
    (throw, tuple(tuple(canon_syllable(sound) for sound in pattern) for pattern in patterns))
    for throw, patterns in THROW_SOUNDS
)


def syllable_stream(text):
    """把一句话变成 (音类, 字下标) 的序列。

    收不进表的字留一个 None 当隔断：不隔断的话，前后两个音节会粘在一起，凭空
    多出一堆不存在的音。英文单词整段跳过 —— 它们归字面那一层管。
    """
    stream = []
    for index, char in enumerate(text):
        syllable = CHAR_SOUND.get(char)
        if syllable is not None:
            stream.append((canon_syllable(syllable), index))
        elif char.isascii():
            continue
        else:
            stream.append((None, index))
    return stream


def _chant_positions(stream):
    """读音流里被口诀占掉的音节下标（从左往右整段扫，不重叠）。"""
    sounds = [syllable for syllable, _ in stream]
    occupied = set()
    index = 0
    while index < len(sounds):
        for key in CHANT_KEYS:
            if tuple(sounds[index:index + len(key)]) == key:
                occupied.update(range(index, index + len(key)))
                index += len(key)
                break
        else:
            index += 1
    return occupied


def _is_negative_bu(entry, pattern, text):
    """「不」后面跟着「对/是/要…」时，它是否定词，不是布。"""
    if pattern != ("bu",):
        return False
    _, index = entry
    if text[index] not in AMBIGUOUS_BU:
        return False
    # 注意取的是切片不是单字：句子末尾没有后续，空字符串是任何字符串的子串。
    tail = text[index + 1:index + 2]
    return bool(tail) and tail in NEGATIVE_TAILS


def find_throw_by_sound(text):
    """按读音找一次出拳。

    返回 (出拳, 它在原文里的字下标, 被口诀占掉的字下标)。找不到就是 (None, None, ...)。
    """
    stream = syllable_stream(text)
    if not stream:
        return None, None, frozenset()

    occupied = _chant_positions(stream)
    covered = frozenset(stream[position][1] for position in occupied)
    kept = [entry for position, entry in enumerate(stream) if position not in occupied]

    best = None
    for start in range(len(kept)):
        for throw, patterns in THROW_KEYS:
            for pattern in patterns:
                end = start + len(pattern)
                if end > len(kept):
                    continue
                if tuple(syllable for syllable, _ in kept[start:end]) != pattern:
                    continue
                if _is_negative_bu(kept[start], pattern, text):
                    continue
                # 取最后一个；同一处命中多个时取更长的那个。
                if best is None or (start, end) >= (best[0], best[1]):
                    best = (start, end, throw)

    if best is None:
        return None, None, covered
    return best[2], kept[best[0]][1], covered


def find_throw_by_chars(text):
    """按字面找出拳，取最后一个，返回 (字下标, 出拳)。"""
    best_index, best_throw = None, None
    for throw, words in THROW_WORDS:
        for word in words:
            index = text.rfind(word)
            if index < 0:
                continue
            if best_index is None or index > best_index:
                best_index, best_throw = index, throw
    return best_index, best_throw


def find_throw(text):
    """在文本里找出拳，取最后一个。

    取最后一个而不是第一个，是为了「我出石头……不对，布」这种自我更正，以及
    「石头剪刀布，我出布」这种口诀在前、出拳在后的说法。

    先按读音找，读音没有结论时再按字面找；两处都命中时取靠后的那个。这样「布」被
    写成「不」照样认得出，而「石头，不对，布」还是布。
    """
    throw, position, occupied = find_throw_by_sound(text)
    if occupied:
        # 读音认出的口诀把那些字也占掉：识别器把口诀末尾的「布」写成「不」时，
        # 字面那一层不该再把口诀里的「剪刀」当成出拳。
        text = "".join(" " if index in occupied else char for index, char in enumerate(text))
    char_position, char_throw = find_throw_by_chars(text)

    if throw is None:
        return char_throw
    if char_throw is None or position >= char_position:
        return throw
    return char_throw


def parse_utterance(text):
    """把一句话拆成 (`command`, `throw`)。

    两者可以同时存在（「开始，石头剪刀布」），由调用方按当前状态决定用哪个 ——
    这比在这里猜游戏处于什么阶段要简单，也不会把状态机偷偷藏进解析器里。
    """
    normalized = normalize_text(text)
    if not normalized:
        return {"command": None, "throw": None, "text": ""}

    command = None
    if any(word in normalized for word in NEW_SESSION_WORDS):
        command = "new_session"
    elif any(word in normalized for word in START_WORDS):
        command = "start"

    # 先剥口诀再找，否则「石头剪刀布」会被当成出了石头（然后又被当成剪刀和布）。
    return {
        "command": command,
        "throw": find_throw(strip_chants(normalized)),
        "text": normalized,
    }


# --------------------------------------------------------------------------- #
# 汇总：给一格 UI 需要的全部数据
# --------------------------------------------------------------------------- #


def round_record(user_throw, jev_throw):
    """一回合结束后记进 history 的那条。"""
    result = outcome(user_throw, jev_throw)
    return {"user": user_throw, "jev": jev_throw, "result": result}


def history_from_records(records):
    """把 history 规整成 build_state 要的元组序列，顺便挡住坏数据。"""
    tuples = []
    for record in records or []:
        try:
            user, jev = record["user"], record["jev"]
        except (TypeError, KeyError):
            continue
        if user in BEATS and jev in BEATS:
            tuples.append((user, jev, outcome(user, jev)))
    return tuples


def tally(records):
    """胜负统计，给「排名」那一栏用。"""
    counts = {"user": 0, "jev": 0, "tie": 0}
    for record in history_from_records(records):
        counts[record[2]] += 1
    played = counts["user"] + counts["jev"] + counts["tie"]
    return {
        "user": counts["user"],
        "jev": counts["jev"],
        "tie": counts["tie"],
        "played": played,
        "user_rate": (counts["user"] / played) if played else 0.0,
        "jev_rate": (counts["jev"] / played) if played else 0.0,
    }
