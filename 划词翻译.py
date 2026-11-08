# -*- coding: utf-8 -*-
"""
划词翻译 SelectTranslate  v2
============================
鼠标左键拖选任意程序的文字 → 松开 → 自动翻译成中文，光标旁弹小窗。

特点
- 纯 Python 标准库，零依赖，无任何 API Key
- 翻译源：Bing 免费接口（不花钱），长连接复用，约 0.8 秒
- 弹窗点击屏幕任意处即消失
- 单实例保护：重复启动会自动退出

快捷键（全局）
- Ctrl + Alt + P : 暂停 / 恢复划词
- Ctrl + Alt + Q : 退出程序

想改参数 → 看下面「配置区」
"""

import ctypes
import ctypes.wintypes as wt
import gzip
import http.client
import json
import queue
import re
import sys
import threading
import time
import tkinter as tk
import urllib.parse

# ============================================================
# 配置区
# ============================================================
MAX_CHARS = 2000            # 超过这个长度不翻译
MIN_CHARS = 1               # 至少几个字才翻译
DRAG_THRESHOLD = 5          # 鼠标移动多少像素算"拖选"
COPY_WAIT_MS = 180          # 模拟 Ctrl+C 后等多久读剪贴板
COOLDOWN_MS = 350           # 两次取词的最小间隔
POPUP_TIMEOUT_MS = 30000    # 兜底自动关闭（点任意处会立刻关）
POPUP_MAX_WIDTH = 460       # 弹窗最大宽度
SKIP_IF_MOSTLY_CJK = True   # 原文超过一半是中文就跳过

BING_HOST = "cn.bing.com"

# 弹窗底部「恢复使用」那行要显示的路径，换成你自己的程序位置即可。
# 打包成 exe 的就填 exe 的路径，跑源码的就填 .py 的路径。
APP_PATH = "C:\\Tools\\SelectTranslate.exe"

NOTE = ("永久关闭：Win+R → 输入 shell:startup → 删除「划词翻译」快捷方式\n"
        "恢复使用：双击 " + APP_PATH)

# ============================================================
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
WH_MOUSE_LL = 14
WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN = 0x0201, 0x0204, 0x0207
WM_LBUTTONUP, WM_MOUSEWHEEL = 0x0202, 0x020A
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL = 0x0001, 0x0002
VK_C, VK_CONTROL = 0x43, 0x11

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
ULONG_PTR = ctypes.c_size_t


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", wt.DWORD),
                ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


# ---- 64 位必须声明返回类型，否则句柄会被截断成 32 位 ----
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
kernel32.GlobalUnlock.restype = wt.BOOL
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE

user32.OpenClipboard.argtypes = [wt.HWND]
user32.OpenClipboard.restype = wt.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wt.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wt.BOOL
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetClipboardData.restype = wt.HANDLE
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HMODULE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
user32.UnhookWindowsHookEx.restype = wt.BOOL
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.RegisterHotKey.restype = wt.BOOL
user32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ULONG_PTR]
user32.keybd_event.restype = None

# ---- 单实例保护：已有实例在跑就静默退出 ----
_MUTEX = kernel32.CreateMutexW(None, False, "SelectTranslate_OnlyOne")
if ctypes.get_last_error() == 183:          # ERROR_ALREADY_EXISTS
    raise SystemExit(0)


# ============================================================
# 剪贴板
# ============================================================
def clip_get_text():
    for _ in range(12):
        if user32.OpenClipboard(None):
            try:
                h = user32.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return ""
                p = kernel32.GlobalLock(h)
                if not p:
                    return ""
                try:
                    return ctypes.wstring_at(p)
                finally:
                    kernel32.GlobalUnlock(h)
            finally:
                user32.CloseClipboard()
        time.sleep(0.012)
    return ""


def clip_set_text(text):
    if not text:
        return
    for _ in range(12):
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                size = (len(text) + 1) * 2
                h = kernel32.GlobalAlloc(0x0042, size)
                if not h:
                    return
                p = kernel32.GlobalLock(h)
                ctypes.memmove(p, ctypes.create_unicode_buffer(text), size)
                kernel32.GlobalUnlock(h)
                user32.SetClipboardData(CF_UNICODETEXT, h)
                return
            finally:
                user32.CloseClipboard()
        time.sleep(0.012)


def send_ctrl_c():
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_C, 0, 0, 0)
    user32.keybd_event(VK_C, 0, 2, 0)
    user32.keybd_event(VK_CONTROL, 0, 2, 0)


# ============================================================
# Bing 免费翻译（无 Key，长连接复用）
# ============================================================
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0")

_conn = None
_tok = {"ig": "", "iid": "", "key": "", "token": "", "ts": 0.0}
_conn_lock = threading.Lock()


def _request(method, path, body=None, headers=None):
    """走长连接；断了自动重连一次。"""
    global _conn
    with _conn_lock:
        for attempt in (0, 1):
            try:
                if _conn is None:
                    _conn = http.client.HTTPSConnection(BING_HOST, timeout=8)
                h = {"User-Agent": UA, "Host": BING_HOST, "Accept-Encoding": "gzip"}
                if headers:
                    h.update(headers)
                _conn.request(method, path, body=body, headers=h)
                resp = _conn.getresponse()
                data = resp.read()
                if resp.getheader("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data.decode("utf-8", "replace")
            except Exception:
                _conn = None
                if attempt:
                    raise


def _bing_token():
    """抓翻译页里的 token，缓存 20 分钟。"""
    now = time.time()
    if _tok["token"] and now - _tok["ts"] < 1200:
        return _tok
    html = _request("GET", "/translator")
    ig = re.search(r'IG:"([^"]+)"', html)
    iid = re.search(r'data-iid="([^"]+)"', html)
    tk = re.search(r'params_AbusePreventionHelper\s*=\s*\[([0-9]+),"([^"]+)"', html)
    if not (ig and iid and tk):
        raise RuntimeError("Bing token 解析失败（网页结构可能变了）")
    _tok.update(ig=ig.group(1), iid=iid.group(1), key=tk.group(1),
                token=tk.group(2), ts=now)
    return _tok


SEP = " ||| "                # 混合文本批量翻译时的分隔符，Bing 会原样保留
_dict_cache = {}


def _bing_translate(text, fromlang):
    """调用 Bing 免费翻译接口，返回中文。"""
    global _conn
    for attempt in (0, 1):
        try:
            c = _bing_token()
            body = urllib.parse.urlencode({
                "fromLang": fromlang, "text": text, "to": "zh-Hans",
                "token": c["token"], "key": c["key"],
            }).encode("utf-8")
            path = f"/ttranslatev3?isVertical=1&&IG={c['ig']}&IID={c['iid']}.1"
            raw = _request("POST", path, body=body, headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"https://{BING_HOST}/translator",
            })
            d = json.loads(raw)
            if isinstance(d, list):
                d = d[0]
            return "".join(x["text"] for x in d["translations"]).strip()
        except Exception:
            if attempt:
                raise
            _tok["token"] = ""      # token 过期，重取一次
            _conn = None


def _dict_def(word):
    """查 Bing 词典，取词条释义；查不到返回空串。翻译接口对缩写常常原样返回，靠这个补。"""
    key = word.lower()
    if key in _dict_cache:
        return _dict_cache[key]
    out = ""
    try:
        html = _request("GET", "/dict/search?q=" + urllib.parse.quote(word) + "&mkt=zh-cn")
        m = re.search(r'<meta name="description" content="必应词典为您提供[^"]{0,40}?的释义，'
                      r'(.{0,200})', html)
        if m:
            d = re.sub(r"\s+", " ", m.group(1).split('" /><')[0]).strip(" ；;，。")
            if "网络释义：" in d:
                d = "网络释义：" + d.split("网络释义：", 1)[1].strip(" ；;，。")
            out = d[:160]
    except Exception:
        out = ""
    _dict_cache[key] = out
    return out


def _same(a, b):
    return re.sub(r"\s+", "", a).casefold() == re.sub(r"\s+", "", b).casefold()


def _translate_mixed(text):
    """中英混合：只把英文片段送去翻译，中文原样保留，这样结果一定是中文。"""
    parts = re.split(r"([\u4e00-\u9fff\u3400-\u4dbf]+)", text)
    idx = [i for i, p in enumerate(parts) if p and LATIN.search(p)]
    if not idx:
        return text
    out = _bing_translate(SEP.join(parts[i].strip() for i in idx), "en")
    got = [x.strip() for x in out.split("|||")]
    if len(got) != len(idx):
        return _bing_translate(text, "auto-detect")     # 回填数量对不上就整段兜底
    for i, g in zip(idx, got):
        raw = parts[i]                                  # 保留原文两端的空格和标点
        lead = raw[:len(raw) - len(raw.lstrip())]
        trail = raw[len(raw.rstrip()):]
        parts[i] = lead + g + trail
    return "".join(parts)


def translate(text):
    """英译中。缩写被 Bing 原样返回时，改用词典释义。"""
    if CJK.search(text) and LATIN.search(text):
        return _translate_mixed(text)
    out = _bing_translate(text, "en")
    if _same(out, text):
        word = text.strip(" \t\r\n，。！？；：、,.!?;:\"'()（）[]【】")
        # 只给缩写查释义（WAF、TCP 这种），普通单词的词典释义往往是「舵手」之类，没意义
        if 2 <= len(word) <= 12 and " " not in word and word.isupper():
            d = _dict_def(word)
            if d:
                return word + "\n" + d
    return out


# ============================================================
# 文本判断
# ============================================================
CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
LATIN = re.compile(r"[A-Za-z]")


def mostly_cjk(s):
    if not SKIP_IF_MOSTLY_CJK:
        return False
    letters = [c for c in s if not c.isspace()]
    if not letters:
        return True
    return len(CJK.findall(s)) / len(letters) > 0.5


def usable(s):
    s = (s or "").strip()
    if not (MIN_CHARS <= len(s) <= MAX_CHARS):
        return None
    if s.count("\n") > 40:
        return None
    if mostly_cjk(s):
        return None
    return s


# ============================================================
# 全局鼠标钩子
# ============================================================
events = queue.Queue()
ui_queue = queue.Queue()
state = {"paused": False, "last_copy": 0.0, "quit": False, "popup_visible": False}

_down_pos = [0, 0]
_down_time = [0.0]
_hook_handle = [None]
_hook_proc = [None]


def _on_mouse(nCode, wParam, lParam):
    if nCode == 0:
        try:
            info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            # 弹窗显示时，点任意处 / 滚轮 → 关掉弹窗
            if state["popup_visible"] and wParam in (
                    WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN, WM_MOUSEWHEEL):
                ui_queue.put(("hide", None))

            if not state["paused"]:
                if wParam == WM_LBUTTONDOWN:
                    _down_pos[0], _down_pos[1] = info.pt.x, info.pt.y
                    _down_time[0] = time.time()
                elif wParam == WM_LBUTTONUP:
                    dx = abs(info.pt.x - _down_pos[0])
                    dy = abs(info.pt.y - _down_pos[1])
                    held = time.time() - _down_time[0]
                    if max(dx, dy) >= DRAG_THRESHOLD and held > 0.08:
                        events.put((info.pt.x, info.pt.y))
        except Exception:
            pass
    return user32.CallNextHookEx(_hook_handle[0], nCode, wParam, lParam)


def hook_thread():
    _hook_proc[0] = HOOKPROC(_on_mouse)
    _hook_handle[0] = user32.SetWindowsHookExW(
        WH_MOUSE_LL, _hook_proc[0], kernel32.GetModuleHandleW(None), 0)
    if not _hook_handle[0]:
        ui_queue.put(("error", ("安装鼠标钩子失败（可能被安全软件拦截）", 200, 200)))
        return

    user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT, ord("P"))
    user32.RegisterHotKey(None, 2, MOD_CONTROL | MOD_ALT, ord("Q"))

    msg = wt.MSG()
    while not state["quit"]:
        r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if r in (0, -1):
            break
        if msg.message == WM_HOTKEY:
            if msg.wParam == 1:
                state["paused"] = not state["paused"]
                ui_queue.put(("toast", "已暂停划词" if state["paused"] else "已恢复划词"))
            elif msg.wParam == 2:
                state["quit"] = True
                ui_queue.put(("quit", None))
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    user32.UnhookWindowsHookEx(_hook_handle[0])


def worker():
    while not state["quit"]:
        try:
            x, y = events.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            now = time.time() * 1000
            if now - state["last_copy"] < COOLDOWN_MS:
                continue
            state["last_copy"] = now

            old = clip_get_text()
            clip_set_text("")
            send_ctrl_c()
            time.sleep(COPY_WAIT_MS / 1000.0)

            got = ""
            for _ in range(8):
                got = clip_get_text()
                if got.strip():
                    break
                time.sleep(0.035)

            if old:
                clip_set_text(old)

            text = usable(got)
            if not text:
                continue

            ui_queue.put(("loading", (x, y)))
            try:
                result = translate(text)
            except Exception as e:
                ui_queue.put(("error", (f"翻译失败：{e}", x, y)))
                continue
            ui_queue.put(("result", (result, x, y)))
        except Exception:
            continue


# ============================================================
# 弹窗
# ============================================================
BG = "#1e2128"
FG = "#e8eaed"
DIM = "#7d8494"
ACCENT = "#5b9dff"
SEP = "#39404e"


class Popup:
    def __init__(self, root):
        self.root = root
        self.win = None
        self.main = None
        self.note = None
        self.hide_job = None

    def _make(self):
        if self.win:
            return
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=ACCENT)
        inner = tk.Frame(w, bg=BG)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        wrap = POPUP_MAX_WIDTH - 24
        self.main = tk.Label(inner, text="", bg=BG, fg=FG, justify="left",
                             anchor="w", wraplength=wrap,
                             font=("Microsoft YaHei UI", 11), padx=12, pady=10)
        self.main.pack(fill="both", expand=True)

        tk.Frame(inner, bg=SEP, height=1).pack(fill="x", padx=12)
        self.note = tk.Label(inner, text=NOTE, bg=BG, fg=DIM, justify="left",
                             anchor="w", wraplength=wrap,
                             font=("Microsoft YaHei UI", 8), padx=12, pady=7)
        self.note.pack(fill="x")

        self.win = w
        w.bind("<Button-1>", lambda e: self.hide())

    def show(self, text, x, y, color=FG):
        self._make()
        self.main.configure(text=text, fg=color)
        self.win.update_idletasks()
        w, h = self.win.winfo_width(), self.win.winfo_height()
        sx, sy = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        px = min(max(8, x + 12), sx - w - 12)
        py = y + 22
        if py + h > sy - 8:
            py = max(8, y - h - 12)
        self.win.geometry(f"+{px}+{py}")
        self.win.deiconify()
        self.win.lift()
        state["popup_visible"] = True
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
        self.hide_job = self.root.after(POPUP_TIMEOUT_MS, self.hide)

    def hide(self):
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
            self.hide_job = None
        if self.win:
            self.win.withdraw()
        state["popup_visible"] = False


def main():
    tkroot = tk.Tk()
    tkroot.withdraw()
    popup = Popup(tkroot)

    threading.Thread(target=hook_thread, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()

    def poll():
        while True:
            try:
                kind, payload = ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "quit":
                state["quit"] = True
                tkroot.quit()
                return
            elif kind == "hide":
                popup.hide()
            elif kind == "loading":
                popup.show("翻译中…", payload[0], payload[1], DIM)
            elif kind == "result":
                popup.show(payload[0], payload[1], payload[2])
            elif kind == "error":
                popup.show(payload[0], payload[1], payload[2], "#ff8080")
            elif kind == "toast":
                popup.show(payload, tkroot.winfo_pointerx(),
                           tkroot.winfo_pointery(), ACCENT)
        tkroot.after(60, poll)

    tkroot.after(60, poll)
    try:
        tkroot.mainloop()
    finally:
        state["quit"] = True
        if _hook_handle[0]:
            user32.UnhookWindowsHookEx(_hook_handle[0])


if __name__ == "__main__":
    main()
