#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""密钥没写在环境变量里的时候，从哪儿来。

顺序是：这个文件，然后才是环境变量。**文件优先**，因为在页面的「设置」里按一次
保存是一个看得见结果的主动动作；让它悄悄输给某个忘掉的 shell 里的 `export`，
是那种要花一小时才查出来的事。

它单独一个文件，而不是塞进别处：这个仓库没有数据库可以放，而一个只躺在同一个
地方、权限 0600 的秘密，比一个会跟着备份和复制到处跑的秘密好推理得多。文件里
只有 key，没有别的。

这里没有任何一条路径会把 key 交回浏览器。`mask()` 是 key 在 HTTP 响应里唯一
出现的地方，它给四位、再给四位 —— 够认出两个 key 不是同一个，不够拿去用。
"""

from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_PATH = os.path.join(ROOT, "var", "i_shoot_rock.settings.json")

PATH_ENV = "I_SHOOT_ROCK_SETTINGS"

# 这个文件只会装这几项。一个什么名字都收的设置文件，等于邀请一次拼错，
# 而拼错的结果是「保存成功，但一直在用旧的」。
FIELDS = ("jev_api_key",)
LABEL = {"jev_api_key": "Jev"}
# 服务端唯一认的两个环境变量名，按提议顺序。`env_hint` 取第一个。
ENV_NAMES = {"jev_api_key": ("JEV_API_KEY", "TYPESAFE_API_KEY")}

MAX_KEY_LENGTH = 512


def path():
    return os.environ.get(PATH_ENV) or DEFAULT_PATH


def load():
    """读出来的 key，或者 {}。**不抛异常** —— 文件坏了就等于没有文件。"""
    try:
        with open(path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    found = {}
    for name in FIELDS:
        value = data.get(name)
        # 只认字符串。这个文件是我们自己写的，里面出现数字或列表只有两种可能：
        # 手改过，或者坏了 —— 而把 123 变成 "123" 只会把这件事故意推迟到很久以后，
        # 以一次 401 的样子出现，穿着普通错误的外衣。
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value:
            found[name] = value
    return found


def save(patch):
    """把一份补丁并进去。空字符串 = 清掉那一项。返回新的状态。

    走临时文件再 rename：写到一半崩掉，不能留下一个「读起来像没有密钥」的半截文件。
    """
    data = load()
    for name in FIELDS:
        if name not in patch:
            continue
        value = str(patch.get(name) or "").strip()
        if value:
            data[name] = value
        else:
            data.pop(name, None)
    target = path()
    if data:
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        temp = target + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
        os.chmod(temp, 0o600)
        os.replace(temp, target)
    elif os.path.exists(target):
        os.remove(target)
    return data


def get(name):
    return load().get(name) or None


def key_source(name):
    """'settings' / 'env' / None —— 现在真正生效的这个 key 是从哪来的。"""
    if get(name):
        return "settings"
    if any(os.environ.get(item) for item in ENV_NAMES[name]):
        return "env"
    return None


def key_value(name):
    """现在生效的 key，或者 None。优先级只有这一处实现。"""
    value = get(name)
    if value:
        return value
    for env_name in ENV_NAMES[name]:
        value = os.environ.get(env_name)
        if value:
            return value.strip()
    return None


def mask(value):
    """四位加四位。够认出两个 key 不同，不够拿去用。"""
    if not value:
        return None
    if len(value) <= 10:
        return "…"
    return "%s…%s" % (value[:4], value[-4:])


def key_status():
    """「设置」这个窗口需要的全部信息，外加一个秘密都没有。"""
    report = {}
    for name in FIELDS:
        source = key_source(name)
        report[name] = {
            "configured": source is not None,
            "source": source,
            "hint": mask(key_value(name)),
            "env_hint": " / ".join(ENV_NAMES[name]),
            "label": LABEL[name],
        }
    report["file"] = path()
    report["file_exists"] = os.path.exists(path())
    return report


def validate(name, value):
    """检查用户粘进来的东西，返回洗干净的值。空字符串的意思就是「清掉」。

    校验针对的是**粘贴事故**，不是格式：API key 是不透明的，除了「有没有跟着
    带进来一个换行或空格」和「这像不像一个 key」，没有别的可查。
    """
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError("%s 必须是字符串" % name)
    value = value.strip()
    if len(value) > MAX_KEY_LENGTH:
        raise ValueError("%s 太长了（超过 %d 个字符）" % (name, MAX_KEY_LENGTH))
    if any(ch.isspace() for ch in value):
        raise ValueError("%s 中间有空白字符 —— 像是粘贴时带上了换行或空格" % name)
    return value


def patch_from(body):
    """HTTP 层送来的 body -> 可以交给 save() 的补丁。不认识的名字直接忽略。"""
    patch = {}
    for name in FIELDS:
        if name in body:
            patch[name] = validate(name, body.get(name))
    return patch
