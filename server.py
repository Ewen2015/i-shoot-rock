#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""石头剪刀布 · 对 Jev —— 本地服务器。

需要它的唯一理由是密钥：Jev 的 API key 不能放到浏览器里。所以浏览器只跟
`127.0.0.1` 说话，真正带 key 的那一次请求从这里发出去。

    export JEV_API_KEY="..."      # 或 TYPESAFE_API_KEY
    python3 server.py             # 然后打开 http://127.0.0.1:8765

接口：

    GET  /                 游戏页面
    POST /api/decide       给历史，换回 Jev 的判断和这一回合的出拳
    POST /api/interpret    一句话 -> 指令/出拳（语音识别之后的解析）
    POST /api/asr          音频 -> 文字（需要配 I_SHOOT_ROCK_ASR_CMD，见 README）
    GET  /api/health       密钥在不在、ASR 通没通、有没有命中缓存

只有标准库，不需要 pip install。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import game

HERE = os.path.dirname(os.path.abspath(__file__))

BASE_URL = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
ENDPOINT = BASE_URL + "/v1/systemone"
MODEL = os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest")
# 这是给人玩的，不是批处理：等两分钟比直接失败更糟。所以每一次尝试有一个超时，
# 整个请求还有一个总预算 —— 无论如何都会在 DEADLINE 秒内给出成功或失败，
# 不会把浏览器晾在那儿。（实测上游会零星 503/502，重试还是必要的。）
TIMEOUT = 20
ATTEMPTS = 3
DEADLINE = float(os.environ.get("I_SHOOT_ROCK_JEV_DEADLINE", "30"))
# 529 是真见过的：上游繁忙时回 {"error_type":"system_overloaded"}。第一版漏了它，
# 于是「上游忙」被当成「请求有毛病」直接放弃了 —— 这两种情况该做的事正好相反。
RETRY_STATUS = (408, 425, 429, 500, 502, 503, 504, 529)

CACHE_ENV = "I_SHOOT_ROCK_CACHE"
CACHE_OFF = ("", "0", "off", "no", "false")
CACHE_PATH = os.environ.get("I_SHOOT_ROCK_CACHE_PATH") or os.path.join(HERE, "i_shoot_rock.jevcache.db")
ASR_ENV = "I_SHOOT_ROCK_ASR_CMD"

MAX_BODY = 32 * 1024 * 1024
STATIC = {"index.html": "text/html; charset=utf-8"}


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #
# 这个端点几乎是纯函数（同一份 state 重问一次，实测相关 0.997），所以把
# (url, body) 哈希后缓存下来，命中的是「重放一个已经发生过的回答」，不是近似。
# 失败一律不写：一次超时被缓存下来会变成永久的错误答案。
class Cache:
    SCHEMA = ("CREATE TABLE IF NOT EXISTS response ("
              "digest TEXT PRIMARY KEY, body TEXT NOT NULL, made_at REAL NOT NULL)")

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def enabled(self):
        value = os.environ.get(CACHE_ENV)
        return True if value is None else value.strip().lower() not in CACHE_OFF

    def _key(self, body):
        return hashlib.sha256((ENDPOINT + "\n").encode() + body).hexdigest()

    def get(self, body):
        if not self.enabled():
            return None
        try:
            with self.lock, sqlite3.connect(self.path, timeout=5) as conn:
                conn.execute(self.SCHEMA)
                row = conn.execute("SELECT body FROM response WHERE digest = ?",
                                   (self._key(body),)).fetchone()
            return json.loads(row[0]) if row else None
        except Exception:  # 缓存坏掉只能导致 miss，不能影响对局
            return None

    def put(self, body, response):
        if not self.enabled() or not storable(response):
            return
        try:
            with self.lock, sqlite3.connect(self.path, timeout=5) as conn:
                conn.execute(self.SCHEMA)
                conn.execute("INSERT OR REPLACE INTO response (digest, body, made_at) VALUES (?, ?, ?)",
                             (self._key(body), json.dumps(response, ensure_ascii=False), time.time()))
        except Exception:
            return


CACHE = Cache(CACHE_PATH)


def storable(response):
    """这个响应值不值得存。

    只认「上游确实答了话」这一种形状。用成功形状来判定，而不是列举失败形状 ——
    将来多出一种没见过的失败时，默认应该是「不存」。顺带挡住非 dict 的 JSON
    （上游回一个裸的字符串或数组并不违法），否则 `.get` 会在这里炸掉。
    """
    return isinstance(response, dict) and bool(response.get("answers"))


# --------------------------------------------------------------------------- #
# Jev
# --------------------------------------------------------------------------- #
def api_key():
    for name in ("JEV_API_KEY", "TYPESAFE_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value.strip()
    return None


class JevError(RuntimeError):
    pass


def ask_jev(state, key, model=MODEL, budget=None):
    """问一次 Jev。返回 (response, latency_ms, cached)。

    重试受总预算约束：`budget` 秒之内要么拿到答案，要么抛错。这是刻意的 ——
    调用方是等在一局游戏中间的真人。
    """
    body = json.dumps({"state": state, "model": model, "questions": game.question()},
                      ensure_ascii=False).encode("utf-8")

    hit = CACHE.get(body)
    if hit is not None:
        return hit, 0, True

    budget = DEADLINE if budget is None else budget
    started = time.time()
    last = "没有成功过一次请求"
    for attempt in range(1, ATTEMPTS + 1):
        left = budget - (time.time() - started)
        if left <= 0:
            last = "%s（已用完 %.0f 秒预算）" % (last, budget)
            break
        request = urllib.request.Request(
            ENDPOINT, data=body, method="POST",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=min(TIMEOUT, left)) as response:
                payload = json.loads(response.read().decode("utf-8"))
            CACHE.put(body, payload)
            return payload, int((time.time() - started) * 1000), False
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            last = "HTTP %s: %s" % (exc.code, detail)
            if exc.code not in RETRY_STATUS:
                break
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = "%s: %s" % (type(exc).__name__, exc)
        if attempt < ATTEMPTS:
            nap = min(0.8 * attempt, max(0.0, budget - (time.time() - started)))
            if nap > 0:
                time.sleep(nap)
    raise JevError(last)


def decide(history, key, model=MODEL):
    """这一局的核心：历史 -> state -> Jev -> 出拳。"""
    records = game.history_from_records(history)
    state = game.build_state(records)
    response, latency_ms, cached = ask_jev(state, key, model)
    decided = game.decide_from_answers(response.get("answers"))
    decided.update({
        "state": state,
        "rounds": len(records),
        "model": response.get("model") or model,
        "usage": response.get("usage"),
        "latency_ms": latency_ms,
        "cached": cached,
        "tally": game.tally(history),
    })
    return decided


# --------------------------------------------------------------------------- #
# 语音识别
# --------------------------------------------------------------------------- #
def asr_command():
    """识别命令，未配置返回 None。

    这里不内置任何一家云厂商的调用：那种代码在这个环境里没法验证，写进去就是
    猜。取而代之的是一个明确的接口 —— `{wav}` 会被替换成临时音频文件路径，
    命令经由 shell 执行，往 stdout 打一行文字即可。README 里有阿里云百炼 `bl`
    的现成配方。
    """
    value = (os.environ.get(ASR_ENV) or "").strip()
    return value or None


def asr_available():
    command = asr_command()
    if not command:
        return False
    return shutil.which(command.split()[0]) is not None


def transcribe(audio, ext="wav"):
    """跑识别命令，返回文字。没有配置就抛 NotImplementedError。

    `ext` 是浏览器录出来的容器格式（MediaRecorder 一般给 webm/opus，不是 wav），
    只用来决定临时文件的后缀 —— 有些识别工具会按后缀猜格式。
    """
    command = asr_command()
    if not command:
        raise NotImplementedError(
            "没有配置语音识别。设置 %s 后重启，例如：\n"
            "  export %s='bl speech recognize --url {wav} --output json'" % (ASR_ENV, ASR_ENV))
    if not asr_available():
        raise NotImplementedError("%s 里的命令不在 PATH 上：%s" % (ASR_ENV, command.split()[0]))

    safe_ext = "".join(c for c in str(ext).lower() if c.isalnum())[:5] or "wav"
    handle, path = tempfile.mkstemp(suffix="." + safe_ext)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(audio)
        # 走 shell：环境变量里放的就是一行命令行，用 .split() 处理引号、管道、
        # 参数里的空格都会出错。这是运行服务器的人自己写的命令，不是外部输入，
        # 而唯一被替换进去的 {wav} 是我们自己 mkstemp 出来的路径，且已加引号。
        result = subprocess.run(command.replace("{wav}", shlex.quote(path)),
                                shell=True, capture_output=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError("识别命令退出码 %s：%s"
                               % (result.returncode, result.stderr.decode(errors="replace")[:300]))
        return result.stdout.decode("utf-8", errors="replace").strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "i-shoot-rock"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 默认的日志太吵，只留一行请求行
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- helpers ----------------------------------------------------------- #
    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        return self.rfile.read(length)

    def read_json(self):
        raw = self.read_body()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    # -- routes ------------------------------------------------------------ #
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/health":
            return self.send_json({
                "ok": True,
                "jev_key": bool(api_key()),
                "model": MODEL,
                "deadline": DEADLINE,
                "asr": "command" if asr_available() else ("configured-but-missing" if asr_command() else "none"),
                "cache": CACHE.enabled(),
            })
        if path == "/favicon.ico":
            # 浏览器一定会来要它；给个空 204，免得控制台一直挂一个 404。
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in ("/", "/index.html"):
            return self.send_file("index.html")
        return self.send_json({"error": "not found"}, 404)

    def send_file(self, name):
        if name not in STATIC:
            return self.send_json({"error": "not found"}, 404)
        full = os.path.join(HERE, name)
        if not os.path.isfile(full):
            return self.send_json({"error": "%s 不存在" % name}, 404)
        with open(full, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", STATIC[name])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            return self.route_post(path)
        except NotImplementedError as exc:
            return self.send_json({"error": str(exc), "not_configured": True}, 501)
        except JevError as exc:
            return self.send_json({"error": "Jev 请求失败：%s" % exc}, 502)
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_json({"error": "请求无法解析：%s" % exc}, 400)
        except Exception as exc:  # noqa: BLE001 - 服务器不该因为一个请求崩掉
            return self.send_json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    def route_post(self, path):
        if path == "/api/decide":
            key = api_key()
            if not key:
                return self.send_json(
                    {"error": "没有找到 Jev 密钥。请 export JEV_API_KEY=... 后重启服务器。"}, 503)
            payload = self.read_json()
            model = (payload.get("model") or MODEL).strip() or MODEL
            return self.send_json(decide(payload.get("history"), key, model))

        if path == "/api/interpret":
            payload = self.read_json()
            return self.send_json(game.parse_utterance(payload.get("text")))

        if path == "/api/asr":
            query = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            text = transcribe(self.read_body(), (query.get("ext") or ["wav"])[0])
            return self.send_json({"text": text, "parsed": game.parse_utterance(text)})

        return self.send_json({"error": "not found"}, 404)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def self_check():
    """`--check`：不发音频、不猜，只确认密钥能不能真的换回一个出拳。"""
    key = api_key()
    if not key:
        print("✗ 没有找到 JEV_API_KEY 或 TYPESAFE_API_KEY")
        return 1
    print("· 密钥已读到（%s…%s）" % (key[:4], key[-4:]))
    try:
        result = decide([{"user": "rock", "jev": "scissors"}] * 3, key)
    except JevError as exc:
        print("✗ 调用失败：%s" % exc)
        return 1
    print("✓ %s  出拳=%s  延迟=%sms%s" % (
        result["model"], game.LABEL[result["throw"]], result["latency_ms"],
        "（缓存）" if result["cached"] else ""))
    print("  它的判断：%s" % json.dumps(result["probabilities"], ensure_ascii=False))
    print("  期望净胜分：%s" % json.dumps(
        {k: round(v, 3) for k, v in result["expected"].items()}, ensure_ascii=False))
    print("  ASR：%s" % ("已配置" if asr_available() else "未配置（可手动出拳）"))
    return 0


def main():
    parser = argparse.ArgumentParser(description="石头剪刀布 · 对 Jev")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--check", action="store_true", help="只测密钥，不起服务器")
    args = parser.parse_args()

    if args.check:
        return self_check()

    if not api_key():
        print("! 没有 JEV_API_KEY / TYPESAFE_API_KEY：页面能开，但每次出拳都会失败。")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print("石头剪刀布 · 对 Jev  ->  http://%s:%d" % (args.host, args.port))
    print("  Jev   %s   %s" % (MODEL, "密钥已就绪" if api_key() else "缺少密钥"))
    print("  语音   %s" % ("识别命令已配置" if asr_available() else "未配置（浏览器语音或手动出拳）"))
    print("  缓存   %s" % ("开" if CACHE.enabled() else "关"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
