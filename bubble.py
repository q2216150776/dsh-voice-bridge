# -*- coding: utf-8 -*-
"""往大肥鱼气泡里推一句话。

用法：
    python bubble.py "正在读取文件…"
    python bubble.py "第 2 步：改代码" --duration 8000

端口自动发现（复用 voice_bridge 的逻辑），DSH 重启换端口也不用改。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import voice_bridge as vb  # noqa: E402

DEFAULT_DURATION = 6000


def say(text: str, duration: int = DEFAULT_DURATION) -> dict:
    text = (text or "").strip()
    if not text:
        return {"ok": False, "reason": "empty"}
    # surface 必填：这台桌宠是 desktop 模式
    path = f"/broadcast?pet=main&surface=desktop"
    res = vb.http("POST", path, {"text": text, "duration": duration}, timeout=8)
    if not res.get("ok"):
        # desktop 不行就退回 web，两个都试过再认输
        res = vb.http("POST", "/broadcast?pet=main&surface=web", {"text": text, "duration": duration}, timeout=8)
    return res


def main(argv: list) -> int:
    duration = DEFAULT_DURATION
    words = []
    i = 0
    while i < len(argv):
        if argv[i] in ("--duration", "-d") and i + 1 < len(argv):
            try:
                duration = int(argv[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        words.append(argv[i])
        i += 1

    text = " ".join(words).strip()
    if not text:
        print("用法: python bubble.py \"要说的话\" [--duration 毫秒]")
        return 2
    res = say(text, duration)
    print(res)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
