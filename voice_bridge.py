# -*- coding: utf-8 -*-
"""
大肥鱼语音桥 —— 点一下桌宠说话，再点一下结束。

═══════════════════════════════════════════════════════════════════════════
一、基础运行原理
═══════════════════════════════════════════════════════════════════════════

    ①  全局鼠标钩子（WH_MOUSE_LL）看到"左键抬起"
                ↓ 只把坐标丢进队列，立刻返回（绝不阻塞鼠标）
    ②  工作线程判断：落点距桌宠中心 < 120px，且该点窗口属于桌宠进程？
                ↓ 是
    ③  waveIn 开始录音（16kHz / 单声道 / PCM16），气泡提示「🎤 听着呢…」
                ↓ 再点一次（或满 20 秒）
    ④  POST /dsh-pet-7340/voice/hear  —— 转文字走宿主自带的 SenseVoice
                ↓
    ⑤  按下面的规则分流（见第二节）
                ↓ 需要交给 DeepSeek 时
    ⑥  切前台 → 点输入框 → Ctrl+V → 回车 → 等回合事件确认

零第三方依赖：只用 ctypes + 标准库。
桌宠（dsh-pet）一行代码都不用改，它只暴露本地 HTTP 接口给我们调。

═══════════════════════════════════════════════════════════════════════════
二、分流规则：优先桥，办不到才交给 DeepSeek
═══════════════════════════════════════════════════════════════════════════

一句话进来，按顺序判断，**任何一步成功就结束，不往下走**：

    优先级 1  说了「交给模型 / 转交 / 通过 deepseek」
              → 直接交给 DeepSeek，桥不插手

    优先级 2  「通过浏览器 / 用浏览器」+ 站点名
              → 桥直接打开浏览器网页            ★ 当前已支持

    优先级 3  「打开 XX 文件夹 / 目录」「打开 X 盘」★ 当前已支持
              → 桥直接打开资源管理器
              （别名表命中 → 目录扫描 → 都找不到才继续往下）

    优先级 4  「打开 XX 客户端 / 软件」
              → 查注册表拿真实 exe，桥直接启动

    优先级 5  前面全都没命中，或者命中但**办失败**了
              → 原样交给 DeepSeek，由 agent 接手

"办失败"包括：目录找不到、文件夹打不开、站点认不出来、客户端启动失败、
一句话里除了"打开 X"还有别的请求（这时不能只做一半）。

→ 设计原则：**桥只做"确定能办到"的事，做不成就把整句话原封不动交出去。**

当前桥的两项主要能力：
    · 打开浏览器网页（内置站点表 + 域名直连）
    · 打开电脑文件夹（别名表 + 目录扫描）

═══════════════════════════════════════════════════════════════════════════
三、几个必须遵守的实现约束（都是踩过的坑）
═══════════════════════════════════════════════════════════════════════════

1. 低级鼠标钩子的回调里**绝对不能有 I/O**
   回调跑在所有鼠标事件的必经路径上：慢 1ms，全系统鼠标慢 1ms；
   超过系统超时还会被 Windows 静默卸载（表现："点了没反应"）。
   → 回调只做 deque.append，命中判定/HTTP/日志全在 _worker 线程。

2. ctypes 里返回指针的 Win32 函数必须声明 restype
   否则 64 位句柄被截断成 32 位，GlobalLock 拿到野指针 → 进程无声消失。

3. DSH 每次启动端口会变
   → resolve_base() 动态发现（netstat / 桌宠日志 / 常见端口），不写死。

4. 输入框位置会随界面状态移动（agent 回话时会下移约 30px）
   → 点击点取两种布局的交集中心，见 COMPOSER_X_RATIO / COMPOSER_BOTTOM_OFFSET。

5. "已发送"不能自己证明自己
   → 桥不写 workStatus，只读；靠 notify 的 turn 事件或 workStatus 变 thinking 判定。

6. 反作弊在跑时不要注入键鼠
   → SGuard / EasyAntiCheat / Vanguard 会把模拟输入当外挂，直接杀进程。
"""
from __future__ import annotations

import base64
import collections
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
from ctypes import wintypes

import ctypes

# --------------------------------------------------------------------------
# 黑匣子：pythonw 没有控制台，崩溃/异常默认全丢。这里把 faulthandler 和
# stdout/stderr 都接到 crash.log，原生访问违例也能留下栈。
# --------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_CRASH_LOG_PATH = os.path.join(_HERE, "crash.log")
try:
    import faulthandler

    try:  # 别让黑匣子自己长成几十 MB
        if os.path.getsize(_CRASH_LOG_PATH) > 2 * 1024 * 1024:
            open(_CRASH_LOG_PATH, "w").close()
    except OSError:
        pass

    _crash_file = open(_CRASH_LOG_PATH, "a", encoding="utf-8", buffering=1)
    faulthandler.enable(file=_crash_file, all_threads=True)
    # pythonw 下 sys.stdout / sys.stderr 是 None；有控制台时（doctor.cmd）保持原样
    if sys.stdout is None or sys.stderr is None:
        sys.stdout = _crash_file
        sys.stderr = _crash_file
except Exception:  # noqa: BLE001
    _crash_file = None

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
BASE = os.environ.get("VOICE_BRIDGE_BASE", "")  # 留空 = 自动发现端口

VERSION = "3.8"  # 3.8: skip input injection while anti-cheat/game processes run

HOT_RADIUS = float(os.environ.get("VOICE_BRIDGE_RADIUS", "120"))   # 判定圈半径(px)
MAX_SECONDS = 20.0                                                 # 上限 24s，留余量
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_BITS = 16

DSH_EXE = os.environ.get("VOICE_BRIDGE_DSH_EXE", "DeepSeek Harness.exe")

# 输入框在窗口里的相对位置（截图实测校准，可用环境变量覆盖）
COMPOSER_X_RATIO = float(os.environ.get("VOICE_BRIDGE_COMPOSER_X", "0.55"))
COMPOSER_BOTTOM_OFFSET = int(os.environ.get("VOICE_BRIDGE_COMPOSER_Y", "130"))
PET_EXE = os.environ.get("VOICE_BRIDGE_PET_EXE", "electron.exe")
PET_WAIT_SECONDS = float(os.environ.get("VOICE_BRIDGE_PET_WAIT", "120"))   # 等桌宠出现的上限
PET_MISS_LIMIT = int(os.environ.get("VOICE_BRIDGE_PET_MISS", "3"))        # 连续几次看不到就退出

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.log")
ALIAS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aliases.json")
LAST_FOLDER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_folder.txt")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
winmm = ctypes.WinDLL("winmm")


def _enable_dpi_awareness() -> None:
    """必须做：否则坐标被系统按 125% 缩放虚拟化，和鼠标钩子的物理像素对不上。"""
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


_enable_dpi_awareness()

# --------------------------------------------------------------------------
# Win32 原型声明：64 位下句柄必须用 c_void_p 接，否则被截断成 32 位直接崩
# --------------------------------------------------------------------------
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.restype = ctypes.c_void_p

user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.SetActiveWindow.argtypes = [ctypes.c_void_p]
user32.SetFocus.argtypes = [ctypes.c_void_p]
user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.GetWindowTextW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]
user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
# 参数类型必须显式声明：lparam 是 64 位指针值，按默认 c_int 传会 OverflowError，
# 回调一抛异常就等于把鼠标事件吞掉了（不传给下一个钩子）。
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]

user32.WindowFromPoint.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


_STATUS_PATH = os.path.join(_HERE, "status.txt")
_status_state: dict[str, str] = {}


def set_status_field(key: str, value: str) -> None:
    """写一份纯 ASCII 的状态文件，给 start.cmd 直接 type 出来看。"""
    _status_state[key] = value
    try:
        lines = [f"{k}={v}" for k, v in _status_state.items()]
        with open(_STATUS_PATH, "w", encoding="ascii", errors="replace") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
_BASE_CACHE = os.path.join(_HERE, "base.txt")
_base_resolved: "str | None" = None


def _probe_base(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/state", timeout=2) as r:
            return b"counter" in r.read(400)
    except Exception:  # noqa: BLE001
        return False


def _ports_of_process(pid: int) -> list:
    ports: list = []
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000,
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[4] == str(pid):
                try:
                    port = int(parts[1].rsplit(":", 1)[1])
                except ValueError:
                    continue
                if port not in ports:
                    ports.append(port)
    except Exception:  # noqa: BLE001
        pass
    return ports


def _ports_from_pet_log() -> list:
    """dsh-pet 的桌面 helper 启动时会把 config URL 写进自己的日志，借它拿端口。"""
    ports: list = []
    try:
        path = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-pet", "logs.txt")
        with open(path, encoding="utf-8", errors="replace") as fh:
            for m in re.finditer(r"127\.0\.0\.1:(\d+)/dsh-pet-7340", fh.read()):
                port = int(m.group(1))
                if port not in ports:
                    ports.append(port)
    except Exception:  # noqa: BLE001
        pass
    return list(reversed(ports))  # 越靠后越新


def resolve_base(force: bool = False) -> str:
    """DSH 每次重启用新端口，这里按优先级试出真正的地址。"""
    global _base_resolved
    if _base_resolved and not force:
        return _base_resolved

    candidates: list = []
    if BASE:
        candidates.append(BASE)
    try:
        with open(_BASE_CACHE, encoding="ascii") as fh:
            cached = fh.read().strip()
        if cached:
            candidates.append(cached)
    except OSError:
        pass

    hwnd = find_window(DSH_EXE)
    pid = window_pid(hwnd) if hwnd else 0
    if pid:
        candidates += [f"http://127.0.0.1:{p}/dsh-pet-7340" for p in _ports_of_process(pid)]
    candidates += [f"http://127.0.0.1:{p}/dsh-pet-7340" for p in _ports_from_pet_log()]
    candidates += [f"http://127.0.0.1:{p}/dsh-pet-7340" for p in (13163, 3080, 8276)]

    seen = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if _probe_base(cand):
            _base_resolved = cand
            try:
                with open(_BASE_CACHE, "w", encoding="ascii") as fh:
                    fh.write(cand)
            except OSError:
                pass
            return cand

    _base_resolved = (candidates[0] if candidates else "http://127.0.0.1:13163/dsh-pet-7340")
    return _base_resolved


def http(method: str, path: str, payload=None, timeout: float = 30.0):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    def _do(base: str):
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return json.loads(body) if body else {}

    try:
        return _do(resolve_base())
    except urllib.error.HTTPError as exc:
        return {"ok": False, "reason": f"http-{exc.code}", "message": exc.read()[:300].decode("utf-8", "replace")}
    except Exception as exc:  # noqa: BLE001
        # 端口可能变了，强制重新发现一次再试
        try:
            return _do(resolve_base(force=True))
        except Exception:  # noqa: BLE001
            return {"ok": False, "reason": "network", "message": str(exc)}


def say_bubble(text: str, duration: int = 5000) -> bool:
    """桥自己的播报走气泡（say 叶子），绝不碰 workStatus——否则会污染校验。"""
    for surface in ("desktop", "web"):
        res = http("POST", f"/broadcast?pet=main&surface={surface}",
                   {"text": text, "duration": duration}, timeout=6)
        if res.get("ok"):
            return True
    return False


def set_status(state: str, task: str | None = None) -> None:
    payload = {"state": state}
    if task is not None:
        payload["task"] = task
    http("POST", "/work-status", payload, timeout=6)


def clear_status() -> None:
    http("DELETE", "/work-status", timeout=6)


def state_snapshot() -> dict:
    return http("GET", "/state", timeout=6) or {}


def current_work_state() -> tuple[str | None, str | None]:
    snap = state_snapshot()
    ws = ((snap.get("sections") or {}).get("workStatus") or {}).get("data") or {}
    return ws.get("state"), ws.get("sessionId")


def notify_counter() -> int:
    snap = state_snapshot()
    return int(((snap.get("sections") or {}).get("notify") or {}).get("counter", 0) or 0)


def wait_for_delivery(before_notify: int, before_state, before_session, timeout: float = 10.0) -> bool:
    """两个独立信号，任一命中就算送达。

    桥自己不再写 workStatus，所以它变成 thinking 一定是 DSH 真的开了新回合；
    notify 则覆盖"桌宠收到会话事件"这条路径。两条都等不到，才报未确认。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.25)
        snap = state_snapshot()
        node = ((snap.get("sections") or {}).get("notify") or {})
        counter = int(node.get("counter", 0) or 0)
        if counter != before_notify:
            event = (node.get("data") or {}).get("event") or {}
            if str(event.get("type", "")).startswith("turn/"):
                return True
        ws = ((snap.get("sections") or {}).get("workStatus") or {}).get("data") or {}
        state = ws.get("state")
        session = ws.get("sessionId")
        if state == "thinking" and session:
            return True
        if session and before_session and session != before_session:
            return True
    return False


def wait_for_turn_start(before_counter: int, timeout: float = 10.0) -> bool:
    """等 DSH 真的开了一轮（桌宠会收到 session/event 的 turn/start）。

    不能用 workStatus 判断——我们自己也会写它，读了等于自证。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.25)
        snap = state_snapshot()
        node = ((snap.get("sections") or {}).get("notify") or {})
        counter = int(node.get("counter", 0) or 0)
        if counter == before_counter:
            continue
        data = node.get("data") or {}
        event = data.get("event") or {}
        if str(event.get("type", "")).startswith("turn/"):
            return True
        before_counter = counter
    return False


# --------------------------------------------------------------------------
# 窗口
# --------------------------------------------------------------------------
def window_pid(hwnd) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_image(pid: int) -> str:
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def find_window(executable: str, restore: bool = True):
    """按进程映像名找顶层窗口，返回 hwnd。

    先找可见窗口；找不到再翻一遍隐藏/最小化的（Electron 应用有时会把主窗口
    藏起来，但进程还在），找到后顺手还原，否则"没找到窗口"会白白放弃一次提交。
    `restore=False` 时只探测存在性，不动窗口（给看门狗用）。
    """
    visible: list = []
    hidden: list = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        name = process_image(window_pid(hwnd)).lower()
        if name != executable.lower():
            return True
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        if not title.value:
            return True
        if user32.IsWindowVisible(hwnd):
            visible.append(hwnd)
            return False
        hidden.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    if visible:
        return visible[0]
    if hidden:
        hwnd = hidden[0]
        if restore:
            user32.ShowWindow(hwnd, 9)   # SW_RESTORE
            time.sleep(0.2)
            log(f"[窗口] 主窗口原本是隐藏的，已还原 hwnd={hwnd}")
        return hwnd
    return None


def pet_alive() -> bool:
    """只看桌宠窗口在不在，不还原、不打扰。"""
    return find_window(PET_EXE, restore=False) is not None


# --------------------------------------------------------------------------
# 反作弊 / 游戏进程保护
# --------------------------------------------------------------------------
# 这些进程在跑时，键鼠注入会被反作弊当成外挂，进程可能被直接杀掉（无日志、无转储）。
ANTI_CHEAT_NAMES = {
    "ace-game", "ace-game-64", "sguard", "sguard64", "sguardsvc", "sguardsvc64",
    "tenprotect", "tenprotect64", "tesafe", "tessafe", "tp3helper",
    "easyanticheat", "easyanticheat_eos", "beservice", "beservice64",
    "vgc", "vgk", "vgtray", "riotclientservices", "riotclientux",
    "gameguard", "xhunter1", "mhyprot2", "mhyprot3", "anticheat",
    "nprotect", "npgg", "game_monitor",
}


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL

TH32CS_SNAPPROCESS = 0x00000002


def running_process_names() -> set:
    names = set()
    try:
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return names
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            while True:
                names.add(entry.szExeFile.lower().replace(".exe", ""))
                if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                    break
        kernel32.CloseHandle(snap)
    except Exception:  # noqa: BLE001
        pass
    return names


def anti_cheat_running() -> bool:
    names = running_process_names()
    if not names:
        return False
    return bool(names & ANTI_CHEAT_NAMES) or any("anticheat" in n for n in names)


# --------------------------------------------------------------------------
# 全屏检测：全屏时挂起（隐藏桌宠 + 卸载钩子）
# --------------------------------------------------------------------------
class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, wintypes.DWORD]
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = ctypes.c_void_p

SHELL_CLASSES = {"Shell_TrayWnd", "Progman", "WorkerW", "Windows.UI.Core.CoreWindow"}


def is_fullscreen_foreground() -> bool:
    """当前前台窗口是否铺满整个显示器（不含桌面/任务栏）。"""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        cls = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, cls, 128)
        if cls.value in SHELL_CLASSES:
            return False
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        mon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not mon:
            return False
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            return False
        m = info.rcMonitor
        mon_w = m.right - m.left
        mon_h = m.bottom - m.top
        if mon_w <= 0 or mon_h <= 0:
            return False
        # 与显示器求交集，覆盖率 ≥99% 才算"铺满屏幕"
        ix = max(0, min(rect.right, m.right) - max(rect.left, m.left))
        iy = max(0, min(rect.bottom, m.bottom) - max(rect.top, m.top))
        if (ix * iy) / float(mon_w * mon_h) < 0.99:
            return False
        # 关键区分：最大化窗口会让出任务栏，真全屏会盖住它（含 DPI 缩放的溢出）
        if rect.bottom < m.bottom - 2:
            return False
        if rect.top > m.top + 2:
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def set_pet_visible(visible: bool) -> None:
    """全屏时把桌宠窗口藏起来，退出全屏再露出来。"""
    try:
        hwnd = find_window(PET_EXE, restore=False)
        if hwnd:
            user32.ShowWindow(hwnd, 5 if visible else 0)  # SW_SHOW / SW_HIDE
    except Exception:  # noqa: BLE001
        pass


def virtual_screen() -> tuple[int, int, int, int]:
    return (
        user32.GetSystemMetrics(76),  # SM_XVIRTUALSCREEN
        user32.GetSystemMetrics(77),  # SM_YVIRTUALSCREEN
        user32.GetSystemMetrics(78),  # SM_CXVIRTUALSCREEN
        user32.GetSystemMetrics(79),  # SM_CYVIRTUALSCREEN
    )


def pet_center() -> tuple[float, float] | None:
    pose = http("GET", "/pose?pet=main", timeout=5)
    if not pose.get("ok"):
        return None
    vx, vy, vw, vh = virtual_screen()
    return vx + float(pose["x"]) * vw, vy + float(pose["y"]) * vh


# --------------------------------------------------------------------------
# 录音（waveIn）
# --------------------------------------------------------------------------
WAVE_FORMAT_PCM = 1
WAVE_MAPPER = -1
CALLBACK_NULL = 0


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p), ("dwBufferLength", wintypes.DWORD),
        ("dwBytesRecorded", wintypes.DWORD), ("dwUser", ctypes.c_size_t),
        ("dwFlags", wintypes.DWORD), ("dwLoops", wintypes.DWORD),
        ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t),
    ]


class Recorder:
    """持续录音，后台线程回收缓冲。stop() 返回 PCM16 bytes。"""

    BUFFERS = 6
    BUFFER_BYTES = SAMPLE_RATE * CHANNELS * SAMPLE_BITS // 8 // 5  # 0.2s

    def __init__(self) -> None:
        self.fmt = WAVEFORMATEX(
            WAVE_FORMAT_PCM, CHANNELS, SAMPLE_RATE,
            SAMPLE_RATE * CHANNELS * SAMPLE_BITS // 8,
            CHANNELS * SAMPLE_BITS // 8, SAMPLE_BITS, 0,
        )
        self.hwi = ctypes.c_void_p()
        self.hdrs = (WAVEHDR * self.BUFFERS)()
        self.raw: list = []
        self.chunks: list[bytes] = []
        self.started = False
        self._lock = threading.Lock()

    def start(self) -> bool:
        rc = winmm.waveInOpen(ctypes.byref(self.hwi), WAVE_MAPPER, ctypes.byref(self.fmt), 0, 0, CALLBACK_NULL)
        if rc != 0:
            log(f"[录音] waveInOpen 失败 rc={rc}")
            return False
        self.raw = [ctypes.create_string_buffer(self.BUFFER_BYTES) for _ in range(self.BUFFERS)]
        for i in range(self.BUFFERS):
            hdr = self.hdrs[i]
            hdr.lpData = ctypes.cast(self.raw[i], ctypes.c_void_p)
            hdr.dwBufferLength = self.BUFFER_BYTES
            hdr.dwFlags = 0
            hdr.dwBytesRecorded = 0
            winmm.waveInPrepareHeader(self.hwi, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))
            winmm.waveInAddBuffer(self.hwi, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))
        if winmm.waveInStart(self.hwi) != 0:
            log("[录音] waveInStart 失败")
            self._close()
            return False
        self.started = True
        threading.Thread(target=self._recycle, daemon=True).start()
        return True

    def _recycle(self) -> None:
        while self.started:
            time.sleep(0.05)
            for i in range(self.BUFFERS):
                hdr = self.hdrs[i]
                if not self.started:
                    return
                if hdr.dwFlags & 0x00000001 and hdr.dwBytesRecorded > 0:  # WHDR_DONE
                    with self._lock:
                        self.chunks.append(self.raw[i].raw[: hdr.dwBytesRecorded])
                    hdr.dwBytesRecorded = 0
                    hdr.dwFlags = 0x00000002  # 只清 DONE，必须保留 WHDR_PREPARED
                    winmm.waveInAddBuffer(self.hwi, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))

    def _close(self) -> None:
        try:
            winmm.waveInClose(self.hwi)
        except Exception:  # noqa: BLE001
            pass
        self.hwi = ctypes.c_void_p()

    def stop(self) -> bytes:
        self.started = False
        time.sleep(0.12)
        winmm.waveInStop(self.hwi)
        winmm.waveInReset(self.hwi)
        for i in range(self.BUFFERS):
            hdr = self.hdrs[i]
            if hdr.dwBytesRecorded > 0:
                with self._lock:
                    self.chunks.append(self.raw[i].raw[: hdr.dwBytesRecorded])
                hdr.dwBytesRecorded = 0
            winmm.waveInUnprepareHeader(self.hwi, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR))
        self._close()
        with self._lock:
            data = b"".join(self.chunks)
            self.chunks = []
        return data


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_BITS // 8)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def transcribe(pcm: bytes) -> tuple[bool, str]:
    if not pcm:
        return False, ""
    audio = base64.b64encode(pcm_to_wav(pcm)).decode("ascii")
    res = http("POST", "/voice/hear", {"audio": audio, "language": "zh"}, timeout=60)
    if res.get("ok"):
        return True, (res.get("text") or "").strip()
    log(f"[识别] 失败 {res.get('reason')} {res.get('message', '')}")
    return False, ""


# --------------------------------------------------------------------------
# 提交到 DSH：写剪贴板 -> 激活窗口 -> Ctrl+V -> Enter
# --------------------------------------------------------------------------
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _set_clipboard_text(text: str) -> None:
    user32.OpenClipboard(None)
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        ptr = kernel32.GlobalLock(h)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(CF_UNICODETEXT, h)
    finally:
        user32.CloseClipboard()


def _get_clipboard_text() -> str:
    if not user32.OpenClipboard(None):
        return ""
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        ptr = kernel32.GlobalLock(h)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def _key(vk: int, up: bool = False) -> None:
    user32.keybd_event(vk, 0, 0x0002 if up else 0, 0)


def _tap(vk: int) -> None:
    _key(vk, False)
    time.sleep(0.02)
    _key(vk, True)


user32.IsHungAppWindow.argtypes = [ctypes.c_void_p]
user32.IsHungAppWindow.restype = wintypes.BOOL


def monitor_hang(hwnd, seconds: float = 15.0) -> None:
    """盯着 DSH 窗口，一旦"无响应"就记下持续多久——用来抓偶发卡死。"""
    deadline = time.time() + seconds
    hung_since = None
    try:
        while time.time() < deadline:
            time.sleep(0.2)
            hung = bool(user32.IsHungAppWindow(hwnd))
            if hung and hung_since is None:
                hung_since = time.time()
                log("[卡顿] DSH 窗口开始无响应…")
            elif not hung and hung_since is not None:
                log(f"[卡顿] DSH 恢复响应，持续 {time.time() - hung_since:.1f}s")
                hung_since = None
        if hung_since is not None:
            log(f"[卡顿] 监控结束时 DSH 仍未响应，已持续 {time.time() - hung_since:.1f}s")
    except Exception as exc:  # noqa: BLE001
        log(f"[卡顿] 监控异常 {exc}")


def _click_at(x: int, y: int) -> None:
    """真实点一下鼠标：只切窗口不点输入框的话，Ctrl+V 会粘到空处。"""
    try:
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.05)
        user32.mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN
        time.sleep(0.04)
        user32.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP
    except Exception as exc:  # noqa: BLE001
        log(f"[提交] 点击失败 {exc}")


def submit_to_dsh(text: str, dry_run: bool = False) -> bool:
    """把文字送进 DSH 输入框并回车。返回是否成功激活窗口。"""
    hwnd = find_window(DSH_EXE)
    if not hwnd:
        log(f"[提交] 没找到 {DSH_EXE} 窗口")
        return False

    saved = _get_clipboard_text()
    if not dry_run:
        _set_clipboard_text(text)

    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    ok_focus = False
    # 1) 常规尝试
    user32.BringWindowToTop(hwnd)
    if user32.SetForegroundWindow(hwnd):
        ok_focus = True
    # 2) 按住 ALT 再抢（系统允许"ALT 按下时"改前台）
    if not ok_focus:
        _key(0x12, False)          # ALT down
        time.sleep(0.03)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.05)
        _key(0x12, True)           # ALT up
        time.sleep(0.1)
    # 3) 仍失败：挂到前台线程的输入队列上再抢
    if user32.GetForegroundWindow() != hwnd:
        fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        cur_thread = kernel32.GetCurrentThreadId()
        attached = user32.AttachThreadInput(cur_thread, fg_thread, True)
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(cur_thread, fg_thread, False)
        time.sleep(0.15)
    # 4) 最后手段：最小化再还原（还原的窗口会拿到前台）
    if user32.GetForegroundWindow() != hwnd:
        user32.ShowWindow(hwnd, 6)   # SW_MINIMIZE
        time.sleep(0.12)
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        time.sleep(0.25)

    time.sleep(0.15)
    fg = user32.GetForegroundWindow()
    ok_focus = ok_focus or fg == hwnd
    log(f"[提交] 前台窗口 {'就是 DSH' if ok_focus else '不是 DSH（hwnd=' + str(fg) + '）'}")
    if dry_run:
        return ok_focus

    # 关键一步：窗口在前台不等于输入框有焦点，先点一下输入区再粘贴。
    # 坐标按截图实测校准：输入框在窗口底部，中心约在窗口高度的 bottom-59px。
    rect = wintypes.RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        width = rect.right - rect.left
        cx = rect.left + int(width * COMPOSER_X_RATIO)
        cy = rect.bottom - COMPOSER_BOTTOM_OFFSET
        _click_at(cx, cy)
        time.sleep(0.25)
        log(f"[提交] 已点击输入区 ({cx},{cy})")

    _key(0x11, False)  # Ctrl down
    _tap(0x56)         # V
    _key(0x11, True)
    time.sleep(0.15)
    _tap(0x0D)         # Enter
    time.sleep(0.25)

    if saved:
        time.sleep(0.3)
        try:
            _set_clipboard_text(saved)
        except Exception:  # noqa: BLE001
            pass
    return True


# --------------------------------------------------------------------------
# 「打开某某网站」直连：桥跑在沙箱外，有权限拉起浏览器
# --------------------------------------------------------------------------
import re

SITE_MAP = {
    # 长键放前面，避免子串抢先命中
    "哔哩哔哩": "https://www.bilibili.com",
    "bilibili": "https://www.bilibili.com",
    "b站": "https://www.bilibili.com",
    "bili": "https://www.bilibili.com",
    "小红书": "https://www.xiaohongshu.com",
    "网易云音乐": "https://music.163.com",
    "网易云": "https://music.163.com",
    "腾讯视频": "https://v.qq.com",
    "爱奇艺": "https://www.iqiyi.com",
    "优酷": "https://www.youku.com",
    "百度": "https://www.baidu.com",
    "知乎": "https://www.zhihu.com",
    "淘宝": "https://www.taobao.com",
    "京东": "https://www.jd.com",
    "微博": "https://weibo.com",
    "抖音": "https://www.douyin.com",
    "豆瓣": "https://www.douban.com",
    "掘金": "https://juejin.cn",
    "github": "https://github.com",
    "谷歌": "https://www.google.com",
    "google": "https://www.google.com",
    "油管": "https://www.youtube.com",
    "youtube": "https://www.youtube.com",
    "推特": "https://x.com",
    "twitter": "https://x.com",
    "stackoverflow": "https://stackoverflow.com",
    "steamdb": "https://steamdb.info",
    "teamdb": "https://steamdb.info",
    "steam db": "https://steamdb.info",
    "steam": "https://store.steampowered.com",
    "steam社区": "https://steamcommunity.com",
    "创意工坊": "https://steamcommunity.com/app/550/workshop/",
    "nexus": "https://www.nexusmods.com",
    "bilibili": "https://www.bilibili.com",
}

OPEN_VERBS = ("打开", "访问", "进入", "登录", "去", "上一下")

DOMAIN_RE = re.compile(r"(?<![\w.-])((?:[a-z0-9][a-z0-9-]*\.)+(?:com|cn|net|org|io|dev|app|co)(?:/[^\s，。,.!?！？]*)?)", re.I)


def detect_open_target(text: str) -> str | None:
    """从一句话里判断"要打开某个网站"，返回 URL；不是这类意图返回 None。

    要求动词出现在站点名之前，避免「把哔哩哔哩的缓存去掉」这种被误判。
    「文件夹」「客户端」这类词出现时直接让位——那些是别的分支的事。
    """
    lowered = text.lower()
    # 说了这些，就不是"打开网站"
    if any(w in lowered for w in ("文件夹", "文件夾", "目录", "目錄", "客户端", "客戶端", "软件", "應用", "应用", "程序")):
        return None

    verb_pos = min((lowered.find(v) for v in OPEN_VERBS if v in lowered), default=-1)
    if verb_pos < 0:
        return None

    # 1) 直接给了域名（在原文上匹配，保留路径大小写，如 BV1xx）
    m = DOMAIN_RE.search(text)
    if m:
        host = m.group(1)
        if lowered.find(host.lower()) >= verb_pos:
            return "https://" + host
        return None

    # 2) 站点名映射（长键优先）
    for name in sorted(SITE_MAP, key=len, reverse=True):
        pos = lowered.find(name)
        if pos >= 0 and pos >= verb_pos:
            return SITE_MAP[name]
    return None


def open_url(url: str) -> bool:
    """用系统默认浏览器打开（桥在沙箱外，这条能成）。"""
    try:
        os.startfile(url)  # noqa: S606 - 只在这台机器上打开用户点名的网址
        return True
    except OSError as exc:
        log(f"[打开] 失败 {exc}")
        return False


# --------------------------------------------------------------------------
# 「打开 XX 文件夹」直连
# --------------------------------------------------------------------------
FOLDER_ALIASES = {
    "视频": ("Videos", "视频"),
    "下载": ("Downloads", "下载"),
    "文档": ("Documents", "文档"),
    "图片": ("Pictures", "图片"),
    "音乐": ("Music", "音乐"),
    "桌面": ("Desktop", "桌面"),
}
FOLDER_SUFFIX = re.compile(r"(?:文件夹|文件夾|目录|目錄|文件夹子)")
FOLDER_VERBS = ("打开", "进入", "打开一下", "去看看", "去", "访问")

SKIP_DIRS = {"windows", "windows.old", "$recycle.bin", "system volume information",
             "node_modules", ".git", "appdata", "program files", "program files (x86)",
             "programdata", "recovery", "perflogs"}

# 语音识别常把 deepseek 念成"地铺seek""deep sick""迪普西克"之类，
# 所以这里既列中文口令，也用正则吃下英文的常见变体。
VIA_AGENT_LITERAL = ("通过deepseek", "交给deepseek", "让deepseek", "用deepseek",
                     "deepseek打开", "让大肥鱼", "叫大肥鱼", "问deepseek",
                     "转交", "交给模型", "让模型", "用模型", "交给助手", "让助手",
                     "让大模型", "用大模型", "走大模型", "让ai", "用ai")

VIA_AGENT_PATTERNS = (
    # deepseek / deep seek / deepsick / deep sick / deep sec —— 语音识别常见的几种走样
    r"deep\s*-?\s*(seek|sick|sik|sec|seck|s[ei]k)",
    # 「通过 地铺seek / 迪普seek」这类音译
    r"(通过|交给|让|用|叫)\s*(地铺|迪普|底普|地普|滴普)\s*(seek|sick|sik)?",
)


def wants_agent(text: str) -> bool:
    lowered = text.lower().replace(" ", "")
    if any(m.replace(" ", "") in lowered for m in VIA_AGENT_LITERAL):
        return True
    raw = text.lower()
    return any(re.search(p, raw) for p in VIA_AGENT_PATTERNS)


# 「通过浏览器」= 明确要浏览器，这一句归桥处理，不再转给 agent
BROWSER_MARKERS = ("通过浏览器", "用浏览器", "浏览器打开", "打开浏览器", "在浏览器里",
                   "浏览器里", "browser")


def wants_browser(text: str) -> bool:
    lowered = text.lower().replace(" ", "")
    return any(m in lowered for m in BROWSER_MARKERS)


def strip_browser_marker(text: str) -> str:
    out = text
    for m in BROWSER_MARKERS:
        for variant in (m, m.upper()):
            out = out.replace(variant, "")
    return out.strip(" 的，。,.!?！？")


def browser_search_url(query: str) -> str:
    """站点表没命中时的兜底：用 Bing 搜一下，保证"通过浏览器"永远有结果。"""
    from urllib.parse import quote
    return "https://www.bing.com/search?q=" + quote(query)


# 语气词/客套话，判断"这句话除了打开 X 还说了别的吗"时要忽略
FILLER_WORDS = ("大肥鱼", "麻烦你", "麻烦", "帮我", "给我", "请", "帮我一下", "一下", "吧", "呢",
                "啊", "嗯", "哦", "的", "了", "在", "把",
                # 这些是"要打开什么"的一部分，不算额外要求
                "客户端", "客戶端", "软件", "应用", "程序", "文件夹", "文件夾", "目录", "目錄", "盘")

# 出现这些词，基本可以确定是复合任务，桥梁办不了
COMPLEX_HINTS = ("然后", "接着", "并且", "顺便", "同时", "之后", "再把", "还要", "顺便把",
                 "下载", "上传", "搜索", "整理", "检查", "分析", "对比", "筛选", "删除",
                 "移动", "复制", "重命名", "压缩", "解压", "安装", "卸载", "如果", "但是")


def is_complex_request(text: str, target_phrases: tuple = ()) -> bool:
    """判断一句话是不是"打开 X + 一堆别的要求"。

    桥只会"打开"，多出来的部分它做不了，硬做只会做错（比如把「打开B站并下载视频」
    简化成打开 B 站）。这种情况交给 agent。

    注意：比较一律用小写——`steamDB` 和 `steamdb` 必须当成同一个词。
    """
    lowered = text.lower()
    # 「A 里面的 B」只需要看 B 那半句，外层目录名不算额外要求
    for marker in ("里面的", "里的", "下面", "下的", "之中的", "中的", "盘的"):
        if marker in lowered:
            lowered = lowered.split(marker)[-1]
            break
    for phrase in target_phrases:
        if phrase:
            lowered = lowered.replace(str(phrase).lower(), "")
    for verb in ("打开", "进入", "访问", "去看看", "去"):
        lowered = lowered.replace(verb, "")
    for marker in BROWSER_MARKERS:
        lowered = lowered.replace(marker.lower(), "")
    for word in FILLER_WORDS:
        lowered = lowered.replace(word, "")
    for word in ("里面", "外边", "那层", "这层"):
        lowered = lowered.replace(word, "")
    leftover = "".join(ch for ch in lowered if ch not in " \t，。,.!?！？、：:；;\"'（）()【】[]")
    if len(leftover) >= 3:
        return True
    return any(hint in text for hint in COMPLEX_HINTS)


def detect_folder_target(text: str):
    """识别「打开 XX 文件夹」。

    返回：('drive','E') / ('name','视频') / ('last','') / None
    ('last','') 表示没说具体名字，用上一次打开的那个。
    """
    lowered = text.lower()
    if not any(v in lowered for v in FOLDER_VERBS):
        return None

    # 先看有没有明确的"XX文件夹"；没有才退回到"X 盘"
    m = re.search(r"(?:打开|进入|访问|去看看|去)\s*(.+)\s*(?:文件夹|文件夾|目录|目錄)", text)
    if m:
        name = m.group(1).strip(" 的了是在，。,.!?！？")
        # 「A 里面的 B」「A 里的 B」「A 盘的 B」→ 只取 B
        for marker in ("里面的", "里的", "下面", "下的", "之中的", "中的", "盘的", "/", "\\"):
            if marker in name:
                name = name.rsplit(marker, 1)[-1].strip(" 的了是在，。,.!?！？")
        VAGUE = ("那个", "这个", "刚才", "上次", "之前", "前面", "上一个", "上一次", "刚刚")
        if name and not any(v in name for v in VAGUE):
            return ("name", name)
        return ("last", "")

    m = re.search(r"([a-z])\s*盘", lowered)
    if m:
        return ("drive", m.group(1).upper())

    # 裸的「打开文件夹」/「打开目录」
    if re.search(r"(?:打开|进入|去)\s*(?:文件夹|文件夾|目录|目錄)", text):
        return ("last", "")
    return None


def _scan_dirs(root: str, max_depth: int, deadline: float):
    """广度优先扫目录，带时间上限，避免卡死。"""
    queue = [(root, 0)]
    while queue:
        if time.time() > deadline:
            return
        path, depth = queue.pop(0)
        try:
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    low = entry.name.lower()
                    if low in SKIP_DIRS or low.startswith("."):
                        continue
                    yield entry.path, depth + 1
                    if depth + 1 < max_depth:
                        queue.append((entry.path, depth + 1))
        except (OSError, PermissionError):
            continue


def find_folder(name: str, timeout: float = 6.0):
    """在用户目录和 D:/E: 下按名字找目录，精确匹配优先。

    先查别名表（含历史打开记录），再扫目录。Steam 游戏库单独给更深的层级，
    因为 addons 这类目录藏在 steamapps/common/<game>/<mod>/ 底下。
    """
    wanted = {name.strip().lower()}
    for cn, ens in FOLDER_ALIASES.items():
        if cn in name.lower():
            wanted |= {e.lower() for e in ens}

    # 1) 别名表：后缀优先，再做包含匹配
    #    「求生之路2的补丁」既含"求生之路2"也含"补丁"，必须让结尾的"补丁"说话。
    aliases = load_aliases()
    norm = name.lower().replace("的", "").replace(" ", "")
    for key in sorted(aliases, key=len, reverse=True):
        if norm.endswith(key) and os.path.isdir(aliases[key]):
            return aliases[key]
    for key in sorted(aliases, key=len, reverse=True):
        if key in norm and os.path.isdir(aliases[key]):
            return aliases[key]

    # 2) 目录扫描
    roots = [
        (os.path.expanduser("~"), 2),
        ("D:\\", 2),
        ("E:\\", 2),
        (r"E:\SteamLibrary\steamapps\common", 3),
        (r"D:\STEAM\steamapps\common", 3),
    ]
    deadline = time.time() + timeout
    fuzzy = None
    for root, depth in roots:
        if not os.path.isdir(root):
            continue
        for path, _d in _scan_dirs(root, depth, deadline):
            base = os.path.basename(path).lower()
            if base in wanted:
                return path
            if fuzzy is None and any(w in base for w in wanted):
                fuzzy = path
    return fuzzy


def load_aliases() -> dict:
    try:
        with open(ALIAS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k).lower(): str(v) for k, v in data.items() if v}
    except (OSError, ValueError):
        return {}


def save_alias(name: str, path: str) -> None:
    """打开成功后记住"名字 → 路径"，下次说同样的名字直接命中。

    名字先做归一化：去掉"的/空格/顿号/斜杠"。否则「求生之路2、补丁」这种
    整句会被当成一个独立别名存下来，以后靠长度压过真正的「补丁」。
    """
    if not name or not path:
        return
    key = name.strip().lower()
    for ch in ("的", " ", "、", "/", "\\", "，", ","):
        key = key.replace(ch, "")
    if not key:
        return
    data = load_aliases()
    data[key] = path
    try:
        with open(ALIAS_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def remember_last_folder(path: str) -> None:
    try:
        with open(LAST_FOLDER_PATH, "w", encoding="utf-8") as fh:
            fh.write(path)
    except OSError:
        pass


def last_folder() -> str | None:
    try:
        with open(LAST_FOLDER_PATH, encoding="utf-8") as fh:
            path = fh.read().strip()
        return path if path and os.path.isdir(path) else None
    except OSError:
        return None


def open_folder(path: str) -> bool:
    try:
        os.startfile(path)  # noqa: S606
        return True
    except OSError as exc:
        log(f"[打开] 文件夹失败 {exc}")
        return False


# --------------------------------------------------------------------------
# 已安装客户端：查注册表拿真实 exe，而不是猜路径
# --------------------------------------------------------------------------
APP_INDEX_PATH = os.path.join(_HERE, "apps.json")


def scan_installed_apps() -> dict:
    """扫注册表 Uninstall 键，返回 {小写名称: exe 路径}，结果缓存一天。"""
    try:
        if time.time() - os.path.getmtime(APP_INDEX_PATH) < 86400:
            with open(APP_INDEX_PATH, encoding="utf-8") as fh:
                return json.load(fh)
    except (OSError, ValueError):
        pass

    import winreg

    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    apps: dict = {}
    for hive, path in roots:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(key)[0]):
            try:
                sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
            except OSError:
                continue
            try:
                display = str(winreg.QueryValueEx(sub, "DisplayName")[0]).strip()
            except OSError:
                continue
            if not display or display.startswith("{"):
                continue
            exe = ""
            for field in ("DisplayIcon", "InstallLocation"):
                try:
                    raw = str(winreg.QueryValueEx(sub, field)[0]).strip().strip('"')
                except OSError:
                    continue
                cand = raw.split(",")[0].strip().strip('"')
                if cand.lower().endswith(".exe") and os.path.isfile(cand):
                    exe = cand
                    break
            if exe:
                apps.setdefault(display.lower(), exe)
    try:
        with open(APP_INDEX_PATH, "w", encoding="utf-8") as fh:
            json.dump(apps, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return apps


def detect_client_target(text: str):
    """「打开 XX 客户端/软件」或「打开 XX」且 XX 是已安装程序 → 返回 (名称, exe)。"""
    lowered = text.lower()
    if not any(v in lowered for v in OPEN_VERBS):
        return None
    apps = scan_installed_apps()
    for name in sorted(apps, key=len, reverse=True):
        if len(name) < 2 or not os.path.isfile(apps[name]):
            continue
        if name.isascii():
            # 英文应用名要卡边界，否则 "steam" 会吃掉 "steamdb"
            if re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", lowered):
                return (name, apps[name])
        elif name in lowered:
            return (name, apps[name])
    return None


def launch_app(exe: str) -> bool:
    try:
        os.startfile(exe)  # noqa: S606
        return True
    except OSError as exc:
        log(f"[打开] 启动失败 {exe}: {exc}")
        return False


# --------------------------------------------------------------------------
# 交互状态机
# --------------------------------------------------------------------------
class Bridge:
    def __init__(self, submit: bool = True) -> None:
        self.submit_enabled = submit
        self.open_sites_enabled = os.environ.get("VOICE_BRIDGE_OPEN_SITES", "1") not in ("0", "false", "off")
        # 认不出的站点默认转给 agent；设成 1 则退回用 Bing 搜一下
        self.search_fallback = os.environ.get("VOICE_BRIDGE_SEARCH_FALLBACK", "0") in ("1", "true", "on")
        self.recording = False
        self.recorder: Recorder | None = None
        self.started_at = 0.0
        self.busy = threading.Lock()

    # -- 由钩子线程调用 ---------------------------------------------------
    def on_pet_click(self) -> None:
        if not self.busy.acquire(blocking=False):
            return
        try:
            self._toggle()
        except Exception:  # noqa: BLE001
            import traceback
            log("[异常] " + traceback.format_exc().replace("\n", " | ")[:600])
            try:
                set_status("error", "内部出错，看 bridge.log")
            except Exception:  # noqa: BLE001
                pass
        finally:
            self.busy.release()

    def _toggle(self) -> None:
        if not self.recording:
            self._start()
        else:
            self._stop()

    def _start(self) -> None:
        rec = Recorder()
        if not rec.start():
            say_bubble("麦克风打不开")
            return
        self.recorder = rec
        self.recording = True
        self.started_at = time.time()
        say_bubble("🎤 听着呢…", duration=MAX_SECONDS * 1000)
        log("[会话] 开始录音")
        threading.Thread(target=self._auto_stop, daemon=True).start()

    def _auto_stop(self) -> None:
        while self.recording and time.time() - self.started_at < MAX_SECONDS:
            time.sleep(0.2)
        if self.recording:
            log(f"[会话] 到达 {MAX_SECONDS}s 上限，自动结束")
            self.on_pet_click()

    def _stop(self) -> None:
        self.recording = False
        rec, self.recorder = self.recorder, None
        pcm = rec.stop() if rec else b""
        secs = len(pcm) / (SAMPLE_RATE * CHANNELS * SAMPLE_BITS // 8)
        log(f"[会话] 结束录音，{secs:.1f}s")
        if secs < 0.4:
            log("[会话] 太短，忽略")
            return

        say_bubble("🧠 识别中…")
        ok, text = transcribe(pcm)
        if not ok or not text:
            say_bubble("没听清，再说一次？", 4000)
            log("[会话] 识别为空")
            return
        log(f"[会话] 识别：{text}")

        # 说了「通过 DeepSeek」之类的话，就整句交给 agent，桥不抢活
        force_agent = wants_agent(text)
        browser_intent = wants_browser(text) and not force_agent
        if force_agent:
            log("[会话] 检测到『交给 DeepSeek』，跳过桥内处理")
        elif browser_intent:
            log("[会话] 检测到『通过浏览器』，由桥直接处理")

        # 分支顺序很重要：文件夹 > 客户端 > 网站。
        # 以前网站判断跑在最前面，导致「网易云文件夹」「网易云客户端」都被当成"打开网易云网站"。
        # 任何一步失败都不原地放弃，而是落下去交给 agent（用户明确要求）。
        if self.open_sites_enabled and not force_agent:
            acted = False

            # A) 文件夹
            folder = detect_folder_target(text)
            if folder:
                kind, value = folder
                if is_complex_request(text, (value, "文件夹", "目录")):
                    say_bubble("这句除了打开还要求别的，交给 DeepSeek", 5000)
                    log(f"[会话] 复杂请求，不做简化处理：{text}")
                else:
                    acted = True
                    path = last_folder() if kind == "last" else (
                        f"{value}:\\" if kind == "drive" else find_folder(value))
                    if path and open_folder(path):
                        say_bubble(f"✅ 已打开 {path}", 6000)
                        log(f"[会话] 直连打开文件夹 {path} -> 成功")
                        remember_last_folder(path)
                        if kind == "name":
                            save_alias(value, path)
                        return
                    reason = "没找到这个文件夹" if not path else "打不开这个文件夹"
                    say_bubble(f"桥{reason}，转给 DeepSeek", 5000)
                    log(f"[会话] 文件夹失败（{reason}）kind={kind} value={value}，转交 DSH")

            # B) 已安装客户端
            if not acted:
                client = detect_client_target(text)
                if client:
                    name, exe = client
                    if is_complex_request(text, (name,)):
                        say_bubble("这句除了打开还要求别的，交给 DeepSeek", 5000)
                        log(f"[会话] 复杂请求，不做简化处理：{text}")
                        acted = True
                    else:
                        acted = True
                        if launch_app(exe):
                            say_bubble(f"✅ 已启动 {name}", 6000)
                            log(f"[会话] 启动客户端 {name} -> 成功")
                            return
                        say_bubble(f"启动 {name} 失败，转给 DeepSeek", 5000)
                        log(f"[会话] 启动客户端失败 {exe}，转交 DSH")

            # C) 网站
            if not acted:
                target = detect_open_target(text)
                searching = False
                if not target and browser_intent and self.search_fallback:
                    query = strip_browser_marker(text)
                    for verb in ("帮我", "给我", "打开", "访问", "进入", "去", "在"):
                        query = query.replace(verb, "")
                    query = query.strip(" 的了，。,.!?！？")
                    if query:
                        target = browser_search_url(query)
                        searching = True
                if target:
                    matched = next((k for k in sorted(SITE_MAP, key=len, reverse=True)
                                    if k in text.lower()), "")
                    if is_complex_request(text, (matched,)):
                        say_bubble("这句除了打开还要求别的，交给 DeepSeek", 5000)
                        log(f"[会话] 复杂请求，不做简化处理：{text}")
                    else:
                        acted = True
                        if open_url(target):
                            if searching:
                                say_bubble(f"🔎 已搜索：{target.split('=')[-1]}", 6000)
                            else:
                                say_bubble(f"✅ 已打开 {target}", 6000)
                            log(f"[会话] 浏览器打开 {target} -> 成功")
                            return
                        say_bubble("浏览器打不开，转给 DeepSeek", 5000)
                        log(f"[会话] 浏览器打开失败 {target}，转交 DSH")
                elif browser_intent:
                    say_bubble("这个站点桥里不认识，交给 DeepSeek", 5000)
                    log("[会话] 站点未识别，转交 DSH")

        if not self.submit_enabled:
            say_bubble(f"（未提交）{text}", 6000)
            return

        before_notify = notify_counter()
        before_state, before_session = current_work_state()
        # 提交时如果上一轮还在跑，新消息可能是"排队/插话"而不是开启新回合，
        # 那样不会有 turn/start，不能拿"没确认"当失败。
        busy_before = before_state in ("thinking", "working")

        # 反作弊在跑时不要注入键鼠：会被当成外挂，进程可能被直接干掉
        if anti_cheat_running():
            _set_clipboard_text(text)
            say_bubble("检测到游戏/反作弊，跳过注入（文字已复制）", 6000)
            log("[会话] 检测到反作弊进程，跳过键鼠注入，文字已放剪贴板")
            return

        # 顺手盯一下 DSH 有没有卡死（偶发问题靠它留证据）
        dsh_hwnd = find_window(DSH_EXE)
        if dsh_hwnd:
            threading.Thread(target=monitor_hang, args=(dsh_hwnd, 15.0), daemon=True).start()

        if not submit_to_dsh(text):
            _set_clipboard_text(text)
            say_bubble("没找到 DSH 窗口，文字已复制", 5000)
            return

        started = wait_for_delivery(before_notify, before_state, before_session, timeout=15.0)
        if started:
            say_bubble(f"✅ 已发送：{text[:40]}", 6000)
            log("[会话] 已确认送达")
        elif busy_before:
            say_bubble(f"已发送（我上一轮还在跑，多半已排队）：{text[:20]}", 6000)
            log("[会话] 提交时上一轮仍在运行，未等到新 turn 事件（大概率已排队）")
        else:
            _set_clipboard_text(text)
            say_bubble(f"已发送但未确认，文字已复制：{text[:24]}", 6000)
            log("[会话] 未收到确认信号（消息可能已送达，文字已放剪贴板）")


# --------------------------------------------------------------------------
# 鼠标钩子
# --------------------------------------------------------------------------
WH_MOUSE_LL = 14
WM_LBUTTONUP = 0x0202
WM_APP_REHOOK = 0x8000 + 1   # 自定义：让主线程重新安装钩子
WM_LBUTTONUP = 0x0202


class MouseHook:
    """全局鼠标钩子。

    铁律：回调（callback）里只做一件事——把坐标塞进队列，然后立刻返回。
    低级钩子跑在所有鼠标事件的必经路径上，回调慢 1ms，全系统的鼠标就慢 1ms；
    超过系统超时（默认几百毫秒）还会被 Windows 直接卸掉，表现为"点了没反应"。
    所以命中判定（含 HTTP 查坐标）、日志、触发会话都必须放到工作线程。
    """

    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge
        self.center = None
        self.center_at = 0.0
        self.pet_pid = 0
        self._proc = None
        self._callback = None
        self._suspended = False   # 全屏时置真，回调直接放行
        self._handle = None
        self._hook_tid = 0
        self._queue: "collections.deque" = collections.deque(maxlen=16)

    def _refresh(self) -> None:
        if time.time() - self.center_at < 2.0:
            return
        self.center_at = time.time()
        try:
            self.center = pet_center()
        except Exception:  # noqa: BLE001
            self.center = None
        if not self.pet_pid:
            h = find_window(PET_EXE)
            if h:
                self.pet_pid = window_pid(h)
                log(f"[钩子] 桌宠进程 pid={self.pet_pid}")

    def _hit(self, x: int, y: int) -> bool:
        self._refresh()
        if not self.center:
            return False
        cx, cy = self.center
        if (x - cx) ** 2 + (y - cy) ** 2 > HOT_RADIUS ** 2:
            return False
        pt = wintypes.POINT(int(x), int(y))
        hwnd = user32.WindowFromPoint(pt)
        if not hwnd:
            return False
        pid = window_pid(hwnd)
        return pid == self.pet_pid or process_image(pid).lower() == PET_EXE.lower()

    def _worker(self) -> None:
        """真正干活的地方：命中判定、网络请求、触发会话，都不在钩子线程里。"""
        while True:
            try:
                x, y = self._queue.popleft()
            except IndexError:
                time.sleep(0.03)
                continue
            try:
                if self._hit(x, y):
                    log(f"[钩子] 命中 ({x},{y})")
                    self.bridge.on_pet_click()
            except Exception as exc:  # noqa: BLE001
                log(f"[钩子] 处理异常 {exc}")

    def _uninstall_hook(self, reason: str = "") -> None:
        if not self._handle:
            return
        try:
            user32.UnhookWindowsHookEx(self._handle)
        except Exception:  # noqa: BLE001
            pass
        self._handle = None
        set_status_field("hook", "uninstalled")
        log(f"[钩子] 已卸载{'（' + reason + '）' if reason else ''}")

    def _install_hook(self) -> bool:
        if self._handle:
            return True
        self._handle = user32.SetWindowsHookExW(WH_MOUSE_LL, self._callback, None, 0)
        if not self._handle:
            log(f"[钩子] 安装失败 err={ctypes.get_last_error()}")
            return False
        log("[钩子] 已安装，点击大肥鱼开始/结束说话（桌宠退出/全屏时自动卸载）")
        set_status_field("hook", "installed")
        return True

    def _watch_fullscreen(self) -> None:
        """全屏时挂起：卸载钩子 + 隐藏桌宠；退出全屏恢复。"""
        was_full = False
        while True:
            time.sleep(1.5)
            try:
                cur = is_fullscreen_foreground()
            except Exception:  # noqa: BLE001
                cur = False
            if cur == was_full:
                continue
            was_full = cur
            if cur:
                self._suspended = True
                self._uninstall_hook("进入全屏")
                set_pet_visible(False)
                http("POST", "/sleep", {"sleeping": True}, timeout=4)
                log("[全屏] 检测到全屏程序：已挂起（钩子卸载、桌宠隐藏）")
            else:
                self._suspended = False
                set_pet_visible(True)
                http("POST", "/sleep", {"sleeping": False}, timeout=4)
                try:
                    user32.PostThreadMessageW(self._hook_tid, WM_APP_REHOOK, 0, 0)
                except Exception:  # noqa: BLE001
                    pass
                log("[全屏] 已退出全屏：恢复（桌宠显示、钩子重建）")

    def _watchdog(self) -> None:
        """桌宠没了就撤钩子退出——不让一个没人用的钩子长期挂在系统鼠标路径上。"""
        missing = 0
        while True:
            time.sleep(2.0)
            if pet_alive():
                missing = 0
                continue
            missing += 1
            if missing >= PET_MISS_LIMIT:   # 连续几次（约 2 秒一次）看不到桌宠
                log("[钩子] 桌宠已退出，卸载钩子并结束进程")
                self._uninstall_hook("桌宠退出")
                try:
                    user32.PostThreadMessageW(self._hook_tid, 0x0012, 0, 0)  # WM_QUIT
                except Exception:  # noqa: BLE001
                    pass
                return

    def run(self) -> None:
        # 先等桌宠出现，再挂钩子——钩子和桌宠同生
        waited = 0.0
        while not pet_alive():
            if waited == 0.0:
                log("[钩子] 还没检测到桌宠，等待它启动…")
            time.sleep(2.0)
            waited += 2.0
            if waited >= PET_WAIT_SECONDS:
                log(f"[钩子] 等了 {PET_WAIT_SECONDS:.0f} 秒仍没有桌宠，退出（不挂空钩子）。")
                return

        self._hook_tid = kernel32.GetCurrentThreadId()
        threading.Thread(target=self._worker, daemon=True).start()
        self._refresh()

        @ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        def callback(code, wparam, lparam):
            # 这里必须快：只把坐标抄进队列，绝不阻塞。
            try:
                if self._suspended:
                    return user32.CallNextHookEx(None, code, wparam, lparam)
                if code >= 0 and wparam == WM_LBUTTONUP:
                    data = ctypes.cast(lparam, ctypes.POINTER(MouseHook._Msll)).contents
                    self._queue.append((data.pt.x, data.pt.y))
            except Exception:  # noqa: BLE001
                pass
            try:
                return user32.CallNextHookEx(None, code, wparam, lparam)
            except Exception:  # noqa: BLE001
                return 0

        self._proc = callback  # 保持引用，否则被 GC 回收会崩
        self._callback = callback
        if not self._install_hook():
            return
        threading.Thread(target=self._watch_fullscreen, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_APP_REHOOK:
                # 全屏结束，需要在这个带消息循环的线程里重建钩子
                self._install_hook()
                continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        log("[钩子] 消息循环结束，进程退出")


# ctypes 结构放在类外也行，这里挂到类上便于引用
class _Msll(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


MouseHook._Msll = _Msll


# --------------------------------------------------------------------------
# 自检 / 入口
# --------------------------------------------------------------------------
def selftest(seconds: float = 2.0) -> int:
    log("=== 自检 ===")
    log(f"/state  -> {json.dumps(state_snapshot(), ensure_ascii=False)[:120]}")
    center = pet_center()
    log(f"/pose 中心 -> {center}")
    log(f"桌宠窗口 -> {find_window(PET_EXE)}  DSH 窗口 -> {find_window(DSH_EXE)}")

    rec = Recorder()
    if not rec.start():
        return 1
    log(f"录音 {seconds}s ...")
    time.sleep(seconds)
    pcm = rec.stop()
    secs = len(pcm) / (SAMPLE_RATE * 2)
    log(f"录到 {len(pcm)} 字节 / {secs:.2f}s")
    ok, text = transcribe(pcm)
    log(f"识别 ok={ok} text={text!r}")
    return 0


def doctor() -> int:
    """体检：把每一步的能力都打出来。"""
    print("=== 大肥鱼语音桥 · 体检 ===")
    problems = 0

    snap = state_snapshot()
    ok_state = bool(snap.get("counter"))
    print(f"[{'OK ' if ok_state else 'FAIL'}] dsh-pet /state  counter={snap.get('counter')}")
    problems += 0 if ok_state else 1

    pose = http("GET", "/pose?pet=main", timeout=5)
    ok_pose = bool(pose.get("ok"))
    print(f"[{'OK ' if ok_pose else 'FAIL'}] /pose  {pose.get('x')},{pose.get('y')} facing={pose.get('facing')}")
    problems += 0 if ok_pose else 1

    pet = find_window(PET_EXE)
    print(f"[{'OK ' if pet else 'FAIL'}] 桌宠窗口 hwnd={pet} pid={window_pid(pet) if pet else 0}")
    problems += 0 if pet else 1

    dsh = find_window(DSH_EXE)
    print(f"[{'OK ' if dsh else 'FAIL'}] DSH 窗口 hwnd={dsh} pid={window_pid(dsh) if dsh else 0}")
    problems += 0 if dsh else 1

    # 麦克风
    rec = Recorder()
    ok_rec = rec.start()
    if ok_rec:
        time.sleep(0.5)
        pcm = rec.stop()
        ok_rec = len(pcm) > 0
        print(f"[{'OK ' if ok_rec else 'FAIL'}] 麦克风 0.5s 录到 {len(pcm)} 字节")
    else:
        print("[FAIL] 麦克风打不开")
    problems += 0 if ok_rec else 1

    # 输入注入能力（沙箱里会被拒）
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    before = (pt.x, pt.y)
    moved = user32.SetCursorPos(before[0] + 1, before[1] + 1)
    user32.GetCursorPos(ctypes.byref(pt))
    after = (pt.x, pt.y)
    input_ok = bool(moved) and after != before
    print(f"[{'OK ' if input_ok else 'FAIL'}] 鼠标注入 SetCursorPos={moved} cursor {before}->{after}")
    if not input_ok:
        print("     ↑ 被沙箱/权限限制。脚本必须用 start.cmd 双击运行，不能在受限环境里跑。")
    problems += 0 if input_ok else 1

    print(f"=== 结论：{problems} 项不通过 ===")
    return 0 if problems == 0 else 1


def _single_instance() -> bool:
    """同名互斥体，防止开两份抢钩子。已在运行则返回 False。"""
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    # 必须先清零：CreateMutexW 成功时不会重置 last error，
    # 不清理就会读到上一次残留的 183 而误判"已在运行"。
    kernel32.SetLastError(0)
    # Local\ 而不是 Global\：Global 命名空间要 SeCreateGlobalPrivilege，
    # 普通用户进程会直接 ACCESS_DENIED，保护形同虚设。
    handle = kernel32.CreateMutexW(None, False, "Local\\dsh-voice-bridge")
    err = ctypes.get_last_error()
    if not handle:
        log(f"单实例检查：创建互斥体失败 err={err}，继续运行")
        return True
    if err == 183:  # ERROR_ALREADY_EXISTS
        return False
    _single_instance.handle = handle  # 保持引用，进程退出前不释放
    return True


def main(argv: list[str]) -> int:
    if "--doctor" in argv:
        return doctor()
    if "--selftest" in argv:
        return selftest()
    if "--test-submit" in argv:
        text = " ".join(a for a in argv[argv.index("--test-submit") + 1:]).strip()
        if not text:
            print("用法: --test-submit <文字>")
            return 2
        ok = submit_to_dsh(text)
        log(f"提交到 DSH：{'已激活并回车' if ok else '未确认激活'}")
        return 0
    if "--focus-test" in argv:
        ok = submit_to_dsh("（忽略这段文字）", dry_run=True)
        log(f"激活测试：{'成功' if ok else '失败'}")
        return 0 if ok else 1
    if "--no-submit" in argv:
        bridge = Bridge(submit=False)
    else:
        bridge = Bridge(submit=True)
    log(f"尝试启动 pid={os.getpid()} 版本=v{VERSION}")
    if not _single_instance():
        log("单实例检查：已有一个语音桥在跑，本次退出。若是旧版本，请先运行 stop.cmd。")
        return 0
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.pid"), "w") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass
    log(f"启动：v{VERSION} base={BASE} 半径={HOT_RADIUS}px 提交={'开' if bridge.submit_enabled else '关'}")
    set_status_field("version", f"v{VERSION}")
    set_status_field("pid", str(os.getpid()))
    set_status_field("started", time.strftime("%Y-%m-%d %H:%M:%S"))
    set_status_field("hook", "pending")
    MouseHook(bridge).run()
    return 0


if __name__ == "__main__":
    try:
        log(f"进程启动 pid={os.getpid()}")
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        import traceback
        log("[致命] " + traceback.format_exc().replace("\n", " | ")[:1500])
        raise
