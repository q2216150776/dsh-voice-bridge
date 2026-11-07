# -*- coding: utf-8 -*-
"""dsh-voice-bridge 功能自测

跑一遍所有不需要真人参与的环节，逐项打 OK / FAIL。

    python selftest.py               # 基础项（不真开文件夹）
    python selftest.py --with-open   # 额外真开一次资源管理器

设计原则：不依赖本机的具体路径。需要目录的地方临时建一个，测完删掉。
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import voice_bridge as vb  # noqa: E402

results: list = []
_workdir = None


def check(name: str, fn) -> None:
    t0 = time.time()
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"异常 {type(exc).__name__}: {exc}"
    dt = (time.time() - t0) * 1000
    results.append((name, ok, detail, dt))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:<16} {detail}  ({dt:.0f}ms)")


def workdir() -> str:
    """临时目录放在脚本旁边：系统 %TEMP% 可能被沙箱限制写不了。"""
    global _workdir
    if _workdir is None:
        base = os.path.dirname(os.path.abspath(__file__))
        _workdir = os.path.join(base, ".selftest-tmp")
        os.makedirs(_workdir, exist_ok=True)
    return _workdir


# ---------- 意图识别 ----------
SITE_CASES = {
    "打开哔哩哔哩。": "https://www.bilibili.com",
    "帮我打开百度": "https://www.baidu.com",
    "去知乎看看": "https://www.zhihu.com",
    "打开 github.com": "https://github.com",
    "把哔哩哔哩的缓存去掉": None,
    "帮我把测试报告整理成表格": None,
}
FOLDER_CASES = {
    "打开视频文件夹": ("name", "视频"),
    "打开 addons 文件夹": ("name", "addons"),
    "打开文件夹": ("last", ""),
    "打开那个文件夹": ("last", ""),
    "打开 D 盘": ("drive", "D"),
    "大肥鱼你真厉害": None,
}


def t_site():
    bad = [f"{k} -> {vb.detect_open_target(k)} (期望 {v})"
           for k, v in SITE_CASES.items() if vb.detect_open_target(k) != v]
    return (not bad), ("全部通过" if not bad else "; ".join(bad))


def t_folder():
    bad = [f"{k} -> {vb.detect_folder_target(k)} (期望 {v})"
           for k, v in FOLDER_CASES.items() if vb.detect_folder_target(k) != v]
    return (not bad), ("全部通过" if not bad else "; ".join(bad))


def t_nested():
    """「A 里面的 B」要取到 B，不能只看第一个"文件夹"。"""
    cases = {
        "打开视频文件夹里面的摄像文件夹。": ("name", "摄像"),
        "打开 C 盘里的图片文件夹": ("name", "图片"),
    }
    bad = [f"{k} -> {vb.detect_folder_target(k)}" for k, v in cases.items()
           if vb.detect_folder_target(k) != v]
    return (not bad), ("正确" if not bad else "; ".join(bad))


# ---------- 别名与记忆 ----------
def t_resolve():
    base = workdir()
    target = os.path.join(base, "示例目录")
    os.makedirs(target, exist_ok=True)
    vb.save_alias("自测示例", target)
    got = vb.find_folder("自测示例")
    ok = bool(got) and os.path.normcase(got) == os.path.normcase(target)
    return ok, (got or "解析失败")


def t_alias_order():
    """后缀优先：说「XX2的补丁」时，结尾的"补丁"要压过前面的"XX2"。"""
    base = workdir()
    game = os.path.join(base, "某游戏2")
    patch = os.path.join(base, "某游戏2补丁")
    os.makedirs(game, exist_ok=True)
    os.makedirs(patch, exist_ok=True)
    vb.save_alias("某游戏2", game)
    vb.save_alias("补丁", patch)
    got = vb.find_folder("某游戏2的补丁")
    ok = bool(got) and os.path.normcase(got) == os.path.normcase(patch)
    return ok, (got or "解析失败")


def t_memory():
    target = workdir()
    vb.remember_last_folder(target)
    ok_last = os.path.normcase(vb.last_folder() or "") == os.path.normcase(target)
    ok_ctx = vb.detect_folder_target("打开刚才的文件夹") == ("last", "")
    return (ok_last and ok_ctx), f"last={ok_last} 上下文={ok_ctx}"


# ---------- 分流 ----------
def t_agent_route():
    must_agent = ["通过deepseek帮我打开视频文件夹", "让 DeepSeek 打开知乎",
                  "交给模型打开补丁", "转交给助手处理", "通过地铺seek打开", "deep sick 打开视频"]
    must_bridge = ["打开视频文件夹", "打开 addons 文件夹", "帮我整理一下补丁", "打开steamdb"]
    bad = [f"{t} 应交给 agent" for t in must_agent if not vb.wants_agent(t)]
    bad += [f"{t} 不该交给 agent" for t in must_bridge if vb.wants_agent(t)]
    return (not bad), ("全部正确" if not bad else "; ".join(bad))


def t_browser_route():
    must = ["用浏览器帮我打开steamDB", "通过浏览器打开哔哩哔哩", "在浏览器里打开知乎"]
    bad = [t for t in must if not vb.wants_browser(t)]
    if vb.detect_open_target("用浏览器帮我打开steamDB") != "https://steamdb.info":
        bad.append("steamDB 解析失败")
    return (not bad), ("全部正确" if not bad else "; ".join(bad))


def t_dispatch():
    """优先级：文件夹 > 客户端 > 网站。"""
    bad = []
    if not vb.detect_folder_target("帮我打开网易云文件夹"):
        bad.append("文件夹没优先")
    if vb.detect_open_target("帮我打开网易云文件夹"):
        bad.append("文件夹被当成网站")
    if vb.detect_client_target("打开steamdb"):
        bad.append("steamdb 被当成客户端")
    if vb.detect_open_target("打开steamdb") != "https://steamdb.info":
        bad.append("steamdb 站点解析失败")
    return (not bad), ("优先级正确" if not bad else "; ".join(bad))


def t_complex():
    simple = [("打开视频文件夹", ("视频",)), ("打开steamdb", ("steamdb",)),
              ("打开视频文件夹里面的摄像文件夹。", ("摄像",)), ("打开 C 盘里的图片文件夹", ("图片",))]
    complex_ = [("打开哔哩哔哩并下载里面的L4D2视频", ("哔哩哔哩",)),
                ("打开SteamDB查一下GTA6", ("steamdb",)),
                ("帮我打开网易云然后放初音未来的歌", ("网易云",))]
    bad = [f"{t} 被误判为复杂" for t, tp in simple if vb.is_complex_request(t, tp)]
    bad += [f"{t} 没识别为复杂" for t, tp in complex_ if not vb.is_complex_request(t, tp)]
    return (not bad), ("判断正确" if not bad else "; ".join(bad))


# ---------- 桌宠接口 ----------
def t_state():
    snap = vb.state_snapshot()
    return bool(snap.get("counter")), f"counter={snap.get('counter')}"


def t_pose():
    p = vb.http("GET", "/pose?pet=main", timeout=6)
    return bool(p.get("ok")), f"facing={p.get('facing')}"


def t_base():
    base = vb.resolve_base(force=True)
    return base.startswith("http://127.0.0.1:"), base


def t_bubble():
    r = vb.say_bubble("自测：气泡通路正常", 4000)
    return bool(r), "ok" if r else "失败"


def t_fullscreen():
    val = vb.is_fullscreen_foreground()
    return isinstance(val, bool), ("当前全屏" if val else "当前非全屏（正常）")


# ---------- 音频 ----------
def t_record():
    rec = vb.Recorder()
    if not rec.start():
        return False, "waveIn 打不开"
    time.sleep(1.0)
    pcm = rec.stop()
    return len(pcm) > 0, f"{len(pcm)} 字节 / {len(pcm) / (vb.SAMPLE_RATE * 2):.2f}s"


def t_asr():
    rec = vb.Recorder()
    if not rec.start():
        return False, "waveIn 打不开"
    time.sleep(1.0)
    pcm = rec.stop()
    ok, text = vb.transcribe(pcm)
    return ok, f"ok={ok} text={text!r}"


# ---------- 权限 ----------
def t_inject():
    from ctypes import wintypes
    pt = wintypes.POINT()
    vb.user32.GetCursorPos(vb.ctypes.byref(pt))
    before = (pt.x, pt.y)
    moved = vb.user32.SetCursorPos(before[0] + 1, before[1] + 1)
    vb.user32.GetCursorPos(vb.ctypes.byref(pt))
    ok = bool(moved) and (pt.x, pt.y) != before
    return ok, ("可用" if ok else "被限制 → 必须从资源管理器双击 start.cmd 运行")


def t_open_folder():
    target = workdir()
    ok = vb.open_folder(target)
    return ok, f"{target} -> {'已打开' if ok else '拒绝访问'}"


def cleanup() -> None:
    global _workdir
    if _workdir and os.path.isdir(_workdir):
        shutil.rmtree(_workdir, ignore_errors=True)
    _workdir = None


def main(argv) -> int:
    print("=== dsh-voice-bridge 自测 ===\n")
    print(f"版本 {vb.VERSION}\n")
    try:
        check("站点识别", t_site)
        check("文件夹识别", t_folder)
        check("嵌套路径", t_nested)
        check("别名解析", t_resolve)
        check("别名后缀优先", t_alias_order)
        check("上下文记忆", t_memory)
        check("交给Agent路由", t_agent_route)
        check("浏览器路由", t_browser_route)
        check("分流优先级", t_dispatch)
        check("复杂命令判定", t_complex)
        check("端口自动发现", t_base)
        check("/state 可达", t_state)
        check("/pose 可达", t_pose)
        check("全屏检测", t_fullscreen)
        check("气泡广播", t_bubble)
        check("录音", t_record)
        check("语音识别", t_asr)
        check("键鼠注入", t_inject)
        if "--with-open" in argv:
            check("打开文件夹", t_open_folder)
    finally:
        cleanup()

    passed = sum(1 for r in results if r[1])
    total = len(results)
    print(f"\n=== {passed}/{total} 通过 ===")
    for name, ok, detail, _dt in results:
        if not ok:
            print(f"  未通过：{name} — {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
