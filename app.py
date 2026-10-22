#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
外设电量监视（托盘常驻 + 设置窗口）

支持设备
  · 由程序目录下的 device/*.json 决定（VID / HID 用法 / 指令 / 回包解析）
  · 自带：迈从 MCHOSE G3 系列鼠标、ATK 等 VIA 衍生协议键盘

托盘图标
  · 电量正常                   → 软件图标（与程序图标一致）
  · 存在低于阈值的设备          → 红色警示图标，并与软件图标交替闪烁
  · 勾选的设备都没检测到        → 软件图标压暗

纯 hidapi 直连，不使用 WebView；设置窗口用系统自带的 tkinter。
"""

import os
import sys
import json
import time
import logging
import logging.handlers
import threading
import ctypes
import uuid
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional, Callable

import hid
from PIL import Image, ImageDraw
import pystray
from pystray import Menu, MenuItem

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

APP_NAME = "外设电量监视"
APP_ID = "PeriphBatteryTray"
APP_VERSION = "1.0.6"
AUTOSTART_FLAG = "--autostart"      # 开机自启时带这个参数，用于区分手动/自动
SUMMARY_MAX_LINES = 4               # 启动通知里最多列几台设备

# 支持的设备由 device/*.json 决定，见文件末尾的「设备配置」一节

# ---------------------------------------------------------------- 路径


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def data_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    p = os.path.join(base, APP_ID)
    os.makedirs(p, exist_ok=True)
    return p


CFG_PATH = os.path.join(app_dir(), "config.json")
LOG_PATH = os.path.join(app_dir(), "app.log")     # 和 exe 放一起，方便用户附带反馈
AI_PROMPT_NAME = "AI提示词-生成设备配置.md"

log = logging.getLogger(APP_ID)
log.setLevel(logging.INFO)

# 日志初始化：程序目录可能不可写（比如放进 C:\Program Files）。
# 这里不弹窗，只记住状态，等 Tk 起来后由 run() 提示用户（需求要求主线程弹窗）。
_LOG_FILE_OK = False
_LOG_FILE_ERR = None
try:
    _handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
    )
    _handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-5s  %(message)s"))
    log.addHandler(_handler)
    _LOG_FILE_OK = True
except Exception as _e:       # noqa: BLE001
    _LOG_FILE_ERR = _e

# ---------------------------------------------------------------- 配置

def ask_yes_no(title: str, text: str, warning: bool = True) -> bool:
    """原生是/否弹窗（置顶，不依赖 Tk）。返回 True 表示用户点了「是」。"""
    try:
        mb = ctypes.windll.user32.MessageBoxW
        mb.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                       ctypes.c_wchar_p, ctypes.c_uint]
        mb.restype = ctypes.c_int
        flags = 0x00000004 | 0x00010000 | 0x00040000      # YESNO | SETFOREGROUND | TOPMOST
        if warning:
            flags |= 0x00000030                            # ICONWARNING
        return mb(None, text, title, flags) == 6           # IDYES
    except Exception as e:
        log.warning("确认弹窗失败: %s", e)
        return False



DEFAULTS = {
    "interval_sec": 60,
    "low_threshold": 20,
    "custom_threshold": 0,      # 0 = 没有自定义阈值
    "alert_repeat_min": 30,
    "start_with_windows": False,
    "notify_on_connect": True,
    # key -> {"enabled": bool, "mode": "auto"|"wired"|"wireless"}
    "device_settings": {},
}

PRESET_THRESHOLDS = (10, 15, 20, 25, 30, 40, 50)
CUSTOM_CHOICE = "自定义…"


CONFIG_EXISTED = os.path.exists(CFG_PATH)      # 首次使用判定：配置文件是否存在
CONFIG_WAS_RESET = False                       # 损坏后被重置过，就不再弹首次引导


def _read_user_config():
    """读配置；返回 (数据, 损坏原因)。文件不存在不算异常。"""
    if not os.path.exists(CFG_PATH):
        return None, None
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:                      # noqa: BLE001
        return None, "JSON 解析失败：%s" % e
    if not isinstance(raw, dict):
        return None, "顶层不是对象（%s）" % type(raw).__name__
    return raw, None


def _handle_broken_config(reason: str) -> dict:
    """配置损坏：问用户是否重置为默认；不重置就退出。"""
    global CONFIG_WAS_RESET
    log.error("配置文件异常：%s（%s）", reason, CFG_PATH)
    text = ("配置文件 config.json 已损坏或格式错误，程序无法正常读取。\n"
            "是否使用默认设置覆盖当前配置文件？\n\n"
            "【是】：直接覆盖为默认配置继续运行\n"
            "【否】：程序立即退出")
    if not ask_yes_no("配置文件异常", text):
        log.info("配置文件异常，用户选择退出程序")
        sys.exit(0)
    fresh = json.loads(json.dumps(DEFAULTS))
    if not save_config(fresh):
        log.error("重置配置文件失败：%s", CFG_PATH)
        ask_yes_no("重置失败",
                   "重置配置文件失败，程序即将退出。\n"
                   "请检查程序目录是否可写，或把程序移动到可读写目录。",
                   warning=True)
        sys.exit(1)
    CONFIG_WAS_RESET = True
    log.info("配置文件异常，用户选择重置为默认配置")
    return fresh


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        user, broken = _read_user_config()
        if broken:
            return _handle_broken_config(broken)
        if user is not None:
            for k, v in user.items():
                if k == "devices":
                    # 旧版按「鼠标/键盘」写死的配置，已被 device_settings 取代
                    continue
                if k == "blink_on_alarm":
                    # 低电量闪烁已改为固定行为，不再提供开关
                    continue
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
    except Exception as e:
        log.warning("配置读取失败，使用默认值: %s", e)
    # 数值字段兜底：配置被写坏或手改成乱七八糟的值时，别让界面炸掉
    for k, dv in (("interval_sec", 60), ("low_threshold", 20),
                  ("custom_threshold", 0), ("alert_repeat_min", 30)):
        try:
            cfg[k] = int(cfg.get(k, dv))
        except Exception:
            cfg[k] = dv
    cfg["low_threshold"] = max(1, min(99, cfg["low_threshold"]))
    cfg["custom_threshold"] = max(0, min(99, cfg["custom_threshold"]))
    if cfg["custom_threshold"] in PRESET_THRESHOLDS:
        cfg["custom_threshold"] = 0      # 和预设重合就没必要单独留一项
    cfg["interval_sec"] = max(2, cfg["interval_sec"])
    cfg["alert_repeat_min"] = max(1, cfg["alert_repeat_min"])
    return cfg


_CFG_SAVE_LOCK = threading.Lock()


def save_config(cfg: dict) -> bool:
    """先写临时文件再替换，写一半失败也不会把原配置毁掉。

    设置窗口和托盘菜单可能同时保存，所以：①整体加锁串行化；
    ②临时文件用唯一名字，避免两个线程互相覆盖/删掉对方正在写的文件；
    ③先做一份快照，避免别的线程在序列化途中改动字典。"""
    tmp = "%s.%s.tmp" % (CFG_PATH, uuid.uuid4().hex[:8])
    with _CFG_SAVE_LOCK:
        try:
            for _ in range(3):
                try:
                    data = json.dumps(dict(cfg), ensure_ascii=False, indent=2)
                    break
                except RuntimeError:      # 另一线程正在改字典，稍等重试
                    time.sleep(0.01)
            else:
                raise RuntimeError("配置在序列化期间被持续修改")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, CFG_PATH)
            return True
        except Exception as e:
            log.warning("配置保存失败: %s", e)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            return False


# 占位：真正的读取放在 main() 里、单实例检查之后。
# 否则第二个实例会先弹「配置损坏」甚至改写配置，再才发现已有实例在跑。
CFG = json.loads(json.dumps(DEFAULTS))

# ---------------------------------------------------------------- 图标

C_WHITE = (255, 255, 255, 255)
C_BOLT = (255, 214, 0, 255)
C_WIRED = (34, 197, 94, 255)
C_ALARM = (229, 57, 53, 255)
C_GREY = (88, 98, 114, 255)
C_WARN = (250, 204, 21, 255)        # 角标黄
C_WARN_INK = (66, 32, 6, 255)       # 角标里的感叹号
C_BADGE_RING = (17, 24, 39, 255)    # 角标描边，和图标主体分开


ICON_GRID = 64          # 设计网格：所有坐标按 64×64 描述
SUPERSAMPLE = 4         # 超采样倍数，缩回目标尺寸后得到抗锯齿边缘
TRAY_BASE = 32          # Windows「默认图标尺寸」= 32px @96dpi


def _draw_icon(painter, size: int) -> Image.Image:
    """在 size×size 上按矢量方式绘制再缩回，保证任意尺寸都清晰。"""
    size = max(8, int(size))
    big = size * SUPERSAMPLE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    painter(ImageDraw.Draw(img), big / float(ICON_GRID))
    return img.resize((size, size), Image.LANCZOS)


def tray_icon_size() -> int:
    """托盘图标的像素尺寸。pystray 用 LR_DEFAULTSIZE 加载，
    即 SM_CXICON（32 逻辑像素），按 DPI 缩放后供原尺寸出图可避免二次重采样。"""
    try:
        return max(16, int(round(TRAY_BASE * system_dpi() / 96.0)))
    except Exception:
        return TRAY_BASE


def icon_wired(size: int = ICON_GRID) -> Image.Image:
    def paint(d: ImageDraw.ImageDraw, k: float):
        d.rounded_rectangle((3 * k, 3 * k, 61 * k, 61 * k), radius=15 * k,
                            fill=C_WIRED)
        d.line((30 * k, 46 * k, 30 * k, 24 * k), fill=C_WHITE,
               width=max(1, int(round(6 * k))))
        d.ellipse((26 * k, 14 * k, 34 * k, 22 * k), fill=C_WHITE)
        d.line((30 * k, 38 * k, 21 * k, 31 * k), fill=C_WHITE,
               width=max(1, int(round(5 * k))))
        d.line((30 * k, 32 * k, 39 * k, 25 * k), fill=C_WHITE,
               width=max(1, int(round(5 * k))))
        d.polygon([(23 * k, 46 * k), (37 * k, 46 * k), (30 * k, 55 * k)],
                  fill=C_WHITE)
        d.polygon([(47 * k, 8 * k), (39 * k, 26 * k), (46 * k, 26 * k),
                   (41 * k, 40 * k), (54 * k, 20 * k), (46 * k, 20 * k),
                   (52 * k, 8 * k)], fill=C_BOLT)
    return _draw_icon(paint, size)


def icon_warn(size: int = ICON_GRID) -> Image.Image:
    """琥珀色感叹号：没有可用的设备配置文件。"""
    def paint(d: ImageDraw.ImageDraw, k: float):
        d.rounded_rectangle((3 * k, 3 * k, 61 * k, 61 * k), radius=15 * k,
                            fill=C_WARN)
        d.rounded_rectangle((28 * k, 14 * k, 36 * k, 38 * k), radius=4 * k,
                            fill=C_WARN_INK)
        d.ellipse((28 * k, 43 * k, 36 * k, 51 * k), fill=C_WARN_INK)
    return _draw_icon(paint, size)


def icon_connected(size: int = ICON_GRID) -> Image.Image:
    def paint(d: ImageDraw.ImageDraw, k: float):
        d.ellipse((3 * k, 3 * k, 61 * k, 61 * k), fill=C_WIRED)
        w = max(1, int(round(7 * k)))
        # 一条三点折线 + joint="curve"：两段粗线在拐点处补圆，
        # 否则各画各的会在勾的底部留下一个缺口
        d.line([(18 * k, 33 * k), (28 * k, 43 * k), (47 * k, 21 * k)],
               fill=C_WHITE, width=w, joint="curve")
    return _draw_icon(paint, size)


def icon_offline(size: int = ICON_GRID) -> Image.Image:
    def paint(d: ImageDraw.ImageDraw, k: float):
        d.rounded_rectangle((3 * k, 3 * k, 61 * k, 61 * k), radius=15 * k,
                            fill=C_GREY)
        d.line((18 * k, 46 * k, 46 * k, 18 * k), fill=C_WHITE,
               width=max(1, int(round(7 * k))))
    return _draw_icon(paint, size)


def icon_alarm(size: int = ICON_GRID) -> Image.Image:
    def paint(d: ImageDraw.ImageDraw, k: float):
        d.rounded_rectangle((3 * k, 3 * k, 61 * k, 61 * k), radius=15 * k,
                            fill=C_ALARM)
        d.rounded_rectangle((28 * k, 14 * k, 36 * k, 38 * k), radius=4 * k,
                            fill=C_WHITE)
        d.ellipse((28 * k, 43 * k, 36 * k, 51 * k), fill=C_WHITE)
    return _draw_icon(paint, size)


TOAST_ICON = 128        # 通知里的圆形图标按 128px 出图（足够 200% 缩放使用）


def save_png(img: Image.Image, name: str) -> str:
    p = os.path.join(data_dir(), name)
    img.save(p, "PNG")
    return p


def app_logo_png() -> str:
    """软件图标 PNG，用作通知左上角（appLogoOverride）。

    优先使用打包内置的 assets/app.png；不可用时退回绘制的 USB 图标。"""
    out = os.path.join(data_dir(), "app_logo.png")
    src = None
    if getattr(sys, "frozen", False):
        cand = os.path.join(getattr(sys, "_MEIPASS", ""), "assets", "app.png")
    else:
        cand = os.path.join(app_dir(), "assets", "app.png")
    if cand and os.path.isfile(cand):
        src = cand
    if src:
        try:
            if (not os.path.isfile(out)
                    or os.path.getmtime(out) < os.path.getmtime(src)):
                shutil.copyfile(src, out)
            return out
        except Exception as e:
            log.warning("软件图标导出失败，使用绘制图标: %s", e)
    return save_png(icon_wired(TOAST_ICON), "app_logo.png")


_APP_ICON_CACHE = {}
BADGE_D = 0.44          # 角标直径占图标边长的比例（面积约 1/5）


def _add_badge(im: Image.Image, size: int) -> Image.Image:
    """右下角加黄色警示角标（感叹号），4 倍超采样后缩回。"""
    S = 4
    big = im.resize((size * S, size * S), Image.LANCZOS)
    d = ImageDraw.Draw(big)
    k = size * S / float(ICON_GRID)
    r = BADGE_D * ICON_GRID / 2.0 * k        # 半径
    cx = cy = (ICON_GRID - r / k - 1.5) * k  # 贴右下角，留一点边
    ring = max(1.0, 2.0 * k)
    d.ellipse((cx - r - ring, cy - r - ring, cx + r + ring, cy + r + ring),
              fill=C_BADGE_RING)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=C_WARN)
    # 感叹号
    d.rounded_rectangle((cx - 0.11 * r * 2, cy - 0.52 * r,
                         cx + 0.11 * r * 2, cy + 0.06 * r),
                        radius=0.11 * r * 2, fill=C_WARN_INK)
    d.ellipse((cx - 0.12 * r * 2, cy + 0.22 * r,
               cx + 0.12 * r * 2, cy + 0.58 * r), fill=C_WARN_INK)
    return big.resize((size, size), Image.LANCZOS)


def app_icon_image(size: int, dim: float = 1.0, badge: bool = False):
    """软件图标（托盘常态显示）。按目标尺寸缩放一次后缓存。

    dim < 1 时整体降低不透明度，用于「设备都没连上」的弱化状态；
    badge=True 时右下角加黄色警示角标，表示通知权限不可用。"""
    key = (int(size), round(dim, 2), bool(badge))
    if key in _APP_ICON_CACHE:
        return _APP_ICON_CACHE[key]
    if getattr(sys, "frozen", False):
        cand = os.path.join(getattr(sys, "_MEIPASS", ""), "assets", "app.png")
    else:
        cand = os.path.join(app_dir(), "assets", "app.png")
    try:
        im = Image.open(cand).convert("RGBA")
        im = im.resize((int(size), int(size)), Image.LANCZOS)
        if dim < 1.0:
            im.putalpha(im.getchannel("A").point(lambda v: int(v * dim)))
        if badge:
            im = _add_badge(im, int(size))
        _APP_ICON_CACHE[key] = im
        return im
    except Exception as e:
        log.warning("软件图标加载失败: %s", e)
        return None


# ---------------------------------------------------------------- 通知

AUMID = APP_ID


def register_aumid(icon_path: str) -> None:
    try:
        import winreg
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Classes\AppUserModelId\%s" % AUMID)
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, icon_path)
        winreg.SetValueEx(key, "IconBackgroundColor", 0, winreg.REG_SZ, "0")
        winreg.CloseKey(key)
    except Exception as e:
        log.warning("AUMID 注册失败: %s", e)


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


# 固定脚本：通知内容从 XML 文件读取，绝不拼进脚本源码（避免设备名里带换行/引号时
# 突破 here-string 边界，把用户可控文本当代码执行）
TOAST_PS = r'''
param(
  [Parameter(Mandatory=$true)][string]$XmlPath,
  [Parameter(Mandatory=$true)][string]$AppId,
  [Parameter(Mandatory=$true)][string]$StatusPath
)
$ErrorActionPreference = "Stop"
try {
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
  [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
  $xml = [System.IO.File]::ReadAllText($XmlPath, [System.Text.Encoding]::UTF8)
  $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
  $doc.LoadXml($xml)
  $toast = [Windows.UI.Notifications.ToastNotification]::new($doc)
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show($toast)
  "OK" | Out-File -FilePath $StatusPath -Encoding utf8
} catch {
  ($_.Exception.GetType().FullName + ": " + $_.Exception.Message) | Out-File -FilePath $StatusPath -Encoding utf8
}
'''


APP_LOGO = ""   # AUMID 图标（通知横幅左上角），App 启动时通过 set_app_logo 设置


def set_app_logo(path: str) -> None:
    global APP_LOGO
    APP_LOGO = path


def _cleanup_legacy_toasts() -> None:
    """删掉旧版本共用固定文件名留下的 toast.ps1 / toast_status.txt。"""
    for name in ("toast.ps1", "toast_status.txt"):
        p = os.path.join(data_dir(), name)
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def _strip_control(text: str) -> str:
    """去掉不可见控制字符（保留换行/制表），避免把奇怪字节塞进 XML。"""
    return "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 0x20)


def show_toast(title: str, msg: str, icon_path: str, silent: bool = False) -> None:
    title = _strip_control(title or "")
    msg = _strip_control(msg or "")
    # src 是 XML 属性值：路径里可能有 & " 等字符，必须转义；
    # 空格/中文按 URI 规则编码，否则 Windows 解析不出来
    from urllib.parse import quote as _quote
    src = "file:///" + _quote((icon_path or "").replace("\\", "/"), safe="/:")
    audio_xml = ('<audio silent="true"/>' if silent else
                 '<audio src="ms-winsoundevent:Notification.Looping.Alarm" loop="false"/>')
    xml = (
        '<toast duration="%s">'
        '<visual><binding template="ToastGeneric">'
        '<text>%s</text><text>%s</text>'
        '<image placement="appLogoOverride" hint-crop="circle" src="%s"/>'
        '</binding></visual>%s'
        '</toast>' % ("short" if silent else "long",
                      _xml_escape(title), _xml_escape(msg),
                      _xml_escape(src), audio_xml)
    )
    # 每条通知用独立临时文件：低电量、接入提示、自检可能同时来自不同线程，
    # 共用一个 toast.ps1 会互相覆盖
    uniq = uuid.uuid4().hex[:8]
    d = data_dir()
    xml_path = os.path.join(d, "toast_%s.xml" % uniq)
    status = os.path.join(d, "toast_%s.status" % uniq)
    path = os.path.join(d, "toast_%s.ps1" % uniq)
    try:
        # 脚本是固定的，通知内容走文件参数，不参与任何字符串拼接
        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(xml)
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write(TOAST_PS)
        proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", path,
             "-XmlPath", xml_path, "-AppId", AUMID, "-StatusPath", status],
            creationflags=0x08000000,
        )
        log.info("已发起通知: %s", title)
    except Exception as e:
        log.warning("通知发送失败: %s", e)
        return

    def cleanup():
        # 等 PowerShell 退出再删，避免删掉还在用的文件
        try:
            proc.wait(timeout=20)
        except Exception:
            pass
        for f in (path, xml_path, status):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass

    threading.Thread(target=cleanup, daemon=True).start()


def diagnose_notifications():
    """读注册表判断通知是否可用（纯读，不会弹任何东西）。"""
    import winreg
    notes = []
    ok = True
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\PushNotifications")
        try:
            v, _ = winreg.QueryValueEx(k, "ToastEnabled")
            if v == 0:
                ok = False
                notes.append("系统通知总开关被关闭（设置 → 系统 → 通知）")
        except FileNotFoundError:
            pass
        winreg.CloseKey(k)
    except OSError:
        pass
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings")
        try:
            v, _ = winreg.QueryValueEx(k, "NOC_GLOBAL_SETTING_TOASTS_ENABLED")
            if v == 0:
                ok = False
                notes.append("通知横幅被关闭（可能开了专注助手）")
        except FileNotFoundError:
            pass
        winreg.CloseKey(k)
    except OSError:
        pass
    # 本程序自己的通知开关（设置 → 系统 → 通知 → 本程序）
    try:
        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\%s" % AUMID)
        try:
            v, _ = winreg.QueryValueEx(k, "Enabled")
            if v == 0:
                ok = False
                notes.append("本程序的通知被单独关闭")
        except FileNotFoundError:
            pass
        winreg.CloseKey(k)
    except OSError:
        pass
    return ok, notes


def notify_setting() -> str:
    """向系统查询本程序的通知设置（纯查询，不会弹任何通知）。

    返回 Enabled / DisabledForApplication / DisabledForUser /
    DisabledByGroupPolicy / DisabledByManifest，失败返回空串。"""
    ps = ('[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, '
          'ContentType=WindowsRuntime] | Out-Null; '
          '[Windows.UI.Notifications.ToastNotificationManager]'
          '::CreateToastNotifier("%s").Setting' % AUMID)
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=6,
            creationflags=0x08000000)          # CREATE_NO_WINDOW，不闪黑框
        out = (r.stdout or "").strip()
        return out.splitlines()[-1].strip() if out else ""
    except Exception as e:
        log.warning("查询通知设置失败: %s", e)
        return ""


DISABLED_TEXT = {
    "DisabledForApplication": "系统设置里本程序的通知被关闭",
    "DisabledForUser": "当前用户的通知总开关被关闭",
    "DisabledByGroupPolicy": "被组策略禁用",
    "DisabledByManifest": "被应用清单限制",
}


def notifications_available():
    """返回 (可用?, 说明)。优先问系统 API，拿不到再退回注册表。"""
    s = notify_setting()
    low = s.lower()
    if low == "enabled":
        return True, []
    if low.startswith("disabled"):
        return False, [DISABLED_TEXT.get(s, "通知权限不可用（%s）" % s)]
    return diagnose_notifications()


TOOLTIP_MAX = 124
NOTIFY_WARN_LINES = [
    "警告：通知权限未开启，警告功能无法工作。",
    "单击此处打开通知设置",
]
NOTIFY_CHECK_SEC = 60       # 通知权限复核间隔


def _with_notify_warning(lines) -> str:
    """设备列表 + 分割线 + 通知权限警告。

    提示框只有 128 字符上限，超长时先砍设备行，保证警告一定完整显示。"""
    return _with_hint(lines, NOTIFY_WARN_LINES)


def _with_hint(lines, hint_lines) -> str:
    """设备列表 + 分割线 + 若干行提示；超长时从末尾砍设备行。"""
    block = "\n".join(["─" * 14] + list(hint_lines))
    keep = list(lines)
    while keep:
        head = "\n".join(keep + (["…"] if len(keep) < len(lines) else []))
        if len(head) + 1 + len(block) <= TOOLTIP_MAX:
            return head + "\n" + block
        keep.pop()
    return block[:TOOLTIP_MAX]


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def system_dpi() -> int:
    """取系统实际 DPI（96 / 120 / 144 / 192…）。"""
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
        if 48 <= dpi <= 480:
            return int(dpi)
    except Exception:
        pass
    try:
        dc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)   # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, dc)
        if 48 <= dpi <= 480:
            return int(dpi)
    except Exception:
        pass
    return 96


# ---------------------------------------------------------------- 蓝牙电量
#
# 蓝牙输入设备不走 HID 报文，电量由系统 PnP 属性给出（BTHLE/BTHENUM 父节点）。
# 只要系统能给电量就当作有效设备，跟 device/*.json 里有没有配置无关。

BT_BATT_KEY = "{104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2"
BT_TTL = 4.0                  # 缓存多久重新查一次（查询只要几毫秒，查勤一点）
_BT_CACHE = {"ts": 0.0, "items": [], "busy": False}
_BT_LOCK = threading.Lock()          # 只保护 busy 标志
_BT_REFRESH = threading.Lock()       # 保证同一时刻只有一次系统查询

# 设备属性键：{104EA319-…} 2 = 电量百分比（Byte），DEVPKEY_NAME = 名称
_PROP_BATTERY = ("104EA319-6EE2-4701-BD47-8DDBF425BBE5", 2)
_PROP_NAME = ("B725F130-47EF-101A-A5F1-02608C9EEBAC", 10)
DEVPROP_TYPE_BYTE = 0x03
DEVPROP_TYPE_STRING = 0x12


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class _DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_uint32)]


def _propkey(guid_str: str, pid: int) -> "_DEVPROPKEY":
    k = _DEVPROPKEY()
    ctypes.memmove(ctypes.byref(k.fmtid), uuid.UUID(guid_str).bytes_le, 16)
    k.pid = pid
    return k


_BT_PROP_BATTERY = None
_BT_PROP_NAME = None
_cfgmgr = None


def _cfg():
    """懒加载 cfgmgr32，避免没用到蓝牙时也去碰系统库。

    显式声明参数/返回类型：虽然 ctypes 默认会把 str 转成 wchar_t*，
    但把类型写清楚能避免以后换 Python 版本或 64 位指针截断时踩坑。"""
    global _cfgmgr, _BT_PROP_BATTERY, _BT_PROP_NAME
    if _cfgmgr is None:
        from ctypes import wintypes
        cm = ctypes.WinDLL("cfgmgr32")
        ULONG = wintypes.ULONG
        LPCWSTR = ctypes.c_wchar_p
        LPWSTR = ctypes.c_wchar_p
        PULONG = ctypes.POINTER(ULONG)
        FAILED = 0

        def sig(name, argtypes):
            fn = getattr(cm, name)
            fn.argtypes = argtypes
            fn.restype = ULONG
            return fn

        sig("CM_Get_Device_ID_List_SizeW", [PULONG, LPCWSTR, ULONG])
        sig("CM_Get_Device_ID_ListW", [LPCWSTR, LPWSTR, ULONG, ULONG])
        sig("CM_Locate_DevNodeW", [PULONG, LPCWSTR, ULONG])
        sig("CM_Get_Device_IDW", [ULONG, LPWSTR, ULONG, ULONG])
        sig("CM_Get_Child", [PULONG, ULONG, ULONG])
        sig("CM_Get_Sibling", [PULONG, ULONG, ULONG])
        sig("CM_Get_DevNode_Status", [PULONG, PULONG, ULONG, ULONG])
        sig("CM_Get_DevNode_PropertyW",
            [ULONG, ctypes.POINTER(_DEVPROPKEY), PULONG,
             ctypes.c_void_p, PULONG, ULONG])
        _cfgmgr = cm
        _BT_PROP_BATTERY = _propkey(*_PROP_BATTERY)
        _BT_PROP_NAME = _propkey(*_PROP_NAME)
    return _cfgmgr


def _devnode_ids(enumerator: str) -> list:
    """列出某个枚举器下【当前在线】的设备实例 ID。

    必须带 CM_GETIDLIST_FILTER_PRESENT(0x100)：否则已断开的设备会以
    「幻影节点」留在列表里，电量还是断线前的旧值，界面就不更新了。"""
    cm = _cfg()
    flags = 0x1 | 0x100          # ENUMERATOR | PRESENT
    size = ctypes.c_uint32(0)
    if cm.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), enumerator, flags) != 0:
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    if cm.CM_Get_Device_ID_ListW(enumerator, buf, size.value, flags) != 0:
        return []
    # size.value 是整个多字符串缓冲的「字符数」（含各个 \x00 和结尾双 nul），
    # 所以按字符数取整段再用 \x00 切成多个实例 ID 是对的
    raw = ctypes.wstring_at(buf, size.value)
    return [s for s in raw.split("\x00") if s]


def _devnode_prop(devinst, key):
    """读一个设备属性，返回 (类型, 原始字节)。

    第一遍只问长度，系统会回 CR_BUFFER_SMALL（不是失败），拿到 size 再读一次。"""
    cm = _cfg()
    ptype = ctypes.c_uint32(0)
    size = ctypes.c_uint32(0)
    cm.CM_Get_DevNode_PropertyW(devinst, ctypes.byref(key),
                                ctypes.byref(ptype), None,
                                ctypes.byref(size), 0)
    if size.value == 0:
        return None, b""
    buf = (ctypes.c_ubyte * size.value)()
    if cm.CM_Get_DevNode_PropertyW(devinst, ctypes.byref(key),
                                   ctypes.byref(ptype),
                                   ctypes.cast(buf, ctypes.c_void_p),
                                   ctypes.byref(size), 0) != 0:
        return None, b""
    return ptype.value, bytes(buf[:size.value])


def _is_live_input(devinst) -> bool:
    """父节点下有没有【真正在用】的 HID 输入设备。

    蓝牙设备断开后，BTHLEDEVICE 服务节点仍会留着、状态甚至还是 started，
    真正消失的是它下面的 HID 集合节点，所以要多看一层。"""
    cm = _cfg()
    DN_STARTED = 0x8

    def started(node) -> bool:
        st = ctypes.c_uint32(0)
        prob = ctypes.c_uint32(0)
        return (cm.CM_Get_DevNode_Status(ctypes.byref(st), ctypes.byref(prob),
                                         node, 0) == 0
                and bool(st.value & DN_STARTED))

    def id_of(node) -> str:
        buf = ctypes.create_unicode_buffer(512)
        if cm.CM_Get_Device_IDW(node, buf, 512, 0) == 0:
            return buf.value.upper()
        return ""

    child = ctypes.c_uint32(0)
    r = cm.CM_Get_Child(ctypes.byref(child), devinst, 0)
    while r == 0 and child.value:
        cid = id_of(child)
        if cid.startswith("HID\\") and started(child):
            return True                 # 经典蓝牙：HID 设备直接挂在下面
        if "00001812" in cid:           # BLE 的 HID 服务节点
            grand = ctypes.c_uint32(0)
            r2 = cm.CM_Get_Child(ctypes.byref(grand), child, 0)
            while r2 == 0 and grand.value:
                if id_of(grand).startswith("HID\\") and started(grand):
                    return True         # 还有 HID 集合 → 设备确实在线
                sib2 = ctypes.c_uint32(0)
                r2 = cm.CM_Get_Sibling(ctypes.byref(sib2), grand, 0)
                grand = sib2
        sib = ctypes.c_uint32(0)
        r = cm.CM_Get_Sibling(ctypes.byref(sib), child, 0)
        child = sib
    return False


def _bt_query() -> list:
    """读取所有「有电量的蓝牙输入设备」；直接用 Win32 接口，不起外部进程。"""
    try:
        cm = _cfg()
    except Exception as e:
        log.warning("蓝牙电量不可用: %s", e)
        return []
    items, seen = [], set()
    for enumerator in ("BTHLE", "BTHENUM"):
        try:
            ids = _devnode_ids(enumerator)
        except Exception as e:
            log.debug("枚举 %s 失败: %s", enumerator, e)
            continue
        for iid in ids:
            if not iid.upper().startswith(enumerator + "\\DEV_"):
                continue            # 只要设备级节点，跳过各种服务节点
            devinst = ctypes.c_uint32(0)
            if cm.CM_Locate_DevNodeW(ctypes.byref(devinst), iid, 0) != 0:
                continue
            ptype, data = _devnode_prop(devinst, _BT_PROP_BATTERY)
            if ptype != DEVPROP_TYPE_BYTE or not data:
                continue
            level = data[0]
            if not 0 <= level <= 100:
                continue
            if not _is_live_input(devinst):
                continue            # 耳机、手表，或者已经断开的设备
            ntype, ndata = _devnode_prop(devinst, _BT_PROP_NAME)
            name = ""
            if ntype == DEVPROP_TYPE_STRING and ndata:
                name = ndata.decode("utf-16-le", "ignore").split("\x00")[0].strip()
            addr = iid.split("\\")[1].replace("DEV_", "").replace("Dev_", "")
            key = "bt:" + addr.lower()[:17]
            if key in seen:
                continue
            seen.add(key)
            items.append({"key": key, "name": name or "蓝牙设备",
                          "level": level, "id": iid})
    return items


def _bt_refresh():
    items = _bt_query()
    _BT_CACHE["items"] = items
    _BT_CACHE["ts"] = time.time()
    if items:
        log.debug("蓝牙电量: %s", "、".join(
            "%s %d%%" % (i["name"], i["level"]) for i in items))


def _bt_refresh_once(wait: float = 0.0) -> bool:
    """串行化查询：拿到锁才真的去查，避免两个线程同时刷写缓存。"""
    got = _BT_REFRESH.acquire(True, wait) if wait else _BT_REFRESH.acquire(False)
    if not got:
        return False
    try:
        _bt_refresh()
    finally:
        _BT_REFRESH.release()
    return True


def _kick_bt_refresh():
    with _BT_LOCK:
        if _BT_CACHE["busy"]:
            return
        _BT_CACHE["busy"] = True

    def work():
        try:
            _bt_refresh_once()
        except Exception:
            log.debug("蓝牙电量刷新线程异常", exc_info=True)
        finally:
            with _BT_LOCK:
                _BT_CACHE["busy"] = False

    threading.Thread(target=work, daemon=True).start()


def bt_snapshot(blocking: bool = True) -> list:
    """当前蓝牙输入设备电量；blocking=False 时只在后台刷新，不卡界面。"""
    now = time.time()
    stale = _BT_CACHE["ts"] == 0.0 or now - _BT_CACHE["ts"] > BT_TTL
    if stale:
        if blocking:
            _bt_refresh_once(wait=0.5)     # 别的线程正在查就等它一下
        else:
            _kick_bt_refresh()
    return _BT_CACHE["items"]


# ---------------------------------------------------------------- 设备配置
#
# 电量的读取方式（VID / 特征 / 指令 / 回包解析）全部写在 device/*.json 里，
# 换设备只需加一个文件，不用重新编译。文件格式见 使用说明.txt。

DEVICE_DIR = "device"
PROFILE_KINDS = {"mouse": "鼠标", "keyboard": "键盘", "generic": "外设",
                 "bt": "蓝牙"}


def device_dir() -> str:
    p = os.path.join(app_dir(), DEVICE_DIR)
    try:
        os.makedirs(p, exist_ok=True)
    except Exception as e:
        log.warning("创建 device 目录失败: %s", e)
    return p


def _hex_int(v) -> int:
    """接受 6553 / "0x3837" / "3837" 三种写法。"""
    if isinstance(v, bool):
        raise ValueError("布尔值不能当数字")
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip().replace("0x", "").replace("0X", "")
        if not s or len(s) > 8:
            raise ValueError("无效的十六进制数: %r" % v)
        try:
            return int(s, 16)
        except ValueError:
            raise ValueError("无效的十六进制数: %r" % v)
    raise ValueError("无效的数字: %r" % (v,))


def _hex_bytes(v) -> bytes:
    """接受 "55 30 A5"、"5530A5"、"0x55,0x30" 等写法。"""
    if not isinstance(v, str):
        raise ValueError("必须是十六进制字符串")
    s = v.replace("0X", " ").replace("0x", " ")
    for ch in (",", "-", ";", "|"):
        s = s.replace(ch, " ")
    try:
        return bytes.fromhex(s)
    except ValueError:
        raise ValueError("不是合法的十六进制串: %r" % v)


def _need(d: dict, key: str, where: str):
    if not isinstance(d, dict) or key not in d:
        raise ValueError("缺少字段 %s%s" % (where, key))
    return d[key]


@dataclass
class Profile:
    """一个设备配置文件的解析结果。"""
    name: str
    kind: str
    vid: int
    pids: tuple
    usage_page: int
    usage: int
    payload: bytes
    length: int
    report_size: int
    expect: bytes
    level_index: int
    charge_index: int
    charge_on: tuple
    wired_if: dict
    wired_pids: tuple       # 只有这些 PID 的 0/0 回包才算「纯供电」，空=都算
    alive_if: tuple         # (下标, 值)：回包该字节不等于此值就判定设备已关机
    read_timeout: float
    file: str

    def match_text(self) -> str:
        ids = "/".join("%04X" % p for p in self.pids) if self.pids else "任意"
        return "VID %04X · PID %s · 用法 %04X/%02X" % (
            self.vid, ids, self.usage_page, self.usage)


def load_profile(path: str) -> Profile:
    """读取并严格校验一个配置文件；不合法直接抛 ValueError。"""
    with open(path, "r", encoding="utf-8-sig") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("顶层必须是 JSON 对象")

    name = _need(raw, "name", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name 必须是非空字符串")
    name = name.strip()

    kind = raw.get("kind", "generic")
    if kind not in PROFILE_KINDS:
        raise ValueError("kind 只能是 %s" % " / ".join(PROFILE_KINDS))

    m = _need(raw, "match", "")
    if not isinstance(m, dict):
        raise ValueError("match 必须是对象")
    vid = _hex_int(_need(m, "vid", "match."))
    if not 0 < vid <= 0xFFFF:
        raise ValueError("match.vid 超出范围")
    pids = m.get("pid")
    if pids is None:
        pids = ()
    elif isinstance(pids, (str, int)):
        pids = (_hex_int(pids),)
    elif isinstance(pids, list):
        if not pids:
            raise ValueError("match.pid 数组不能为空")
        pids = tuple(_hex_int(x) for x in pids)
    else:
        raise ValueError("match.pid 必须是数字、字符串或数组")
    if any(not 0 <= x <= 0xFFFF for x in pids):
        raise ValueError("match.pid 必须在 0~FFFF 之间")
    usage_page = _hex_int(_need(m, "usage_page", "match."))
    usage = _hex_int(_need(m, "usage", "match."))
    if not 0 <= usage_page <= 0xFFFF:
        raise ValueError("match.usage_page 必须在 0~FFFF 之间")
    if not 0 <= usage <= 0xFFFF:
        raise ValueError("match.usage 必须在 0~FFFF 之间")

    rq = _need(raw, "request", "")
    if not isinstance(rq, dict):
        raise ValueError("request 必须是对象")
    payload = _hex_bytes(_need(rq, "payload", "request."))
    if not payload:
        raise ValueError("request.payload 不能为空")
    if len(payload) > 64:
        raise ValueError("request.payload 不能超过 64 字节")
    length = _hex_int(rq.get("length", 64))
    if not 1 <= length <= 256:
        raise ValueError("request.length 必须在 1~256 之间")
    if len(payload) > length:
        raise ValueError("request.payload 比 request.length 还长")

    rs = raw.get("response", {})
    if not isinstance(rs, dict):
        raise ValueError("response 必须是对象")
    exp = rs.get("expect", "")
    expect = _hex_bytes(exp) if exp else b""
    level_index = _hex_int(_need(rs, "level_index", "response."))
    charge_index = _hex_int(_need(rs, "charge_index", "response."))
    if len(expect) > length:
        raise ValueError("response.expect 比回包长度还长")
    if not 0 <= level_index < length or not 0 <= charge_index < length:
        raise ValueError("response 的索引必须小于 request.length（%d）" % length)
    charge_on = rs.get("charge_on", [1])
    if (not isinstance(charge_on, list) or not charge_on
            or not all(isinstance(x, int) and not isinstance(x, bool) for x in charge_on)):
        raise ValueError("response.charge_on 必须是非空整数数组")
    if any(not 0 <= x <= 255 for x in charge_on):
        raise ValueError("response.charge_on 的取值必须在 0~255 之间")
    wired_if = rs.get("wired_if", {})
    if not isinstance(wired_if, dict):
        raise ValueError("response.wired_if 必须是对象")
    for k, v in wired_if.items():
        if k not in ("level", "charge"):
            raise ValueError("response.wired_if 只能写 level / charge")
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError("response.wired_if.%s 必须是整数" % k)
        hi = 100 if k == "level" else 255
        if not 0 <= v <= hi:
            raise ValueError("response.wired_if.%s 必须在 0~%d 之间" % (k, hi))
    wp = rs.get("wired_pids")
    if wp is None:
        wired_pids = ()
    elif isinstance(wp, list):
        if not wp:
            raise ValueError("response.wired_pids 不能是空数组")
        wired_pids = tuple(_hex_int(x) for x in wp)
        if any(not 0 <= x <= 0xFFFF for x in wired_pids):
            raise ValueError("response.wired_pids 必须在 0~FFFF 之间")
    else:
        raise ValueError("response.wired_pids 必须是数组")
    ai = rs.get("alive_if")
    if ai is None:
        alive_if = ()
    elif isinstance(ai, dict):
        idx = ai.get("index")
        if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx <= 63:
            raise ValueError("response.alive_if.index 必须是 0~63 的整数")
        val = _hex_int(ai.get("value"))
        if not 0 <= val <= 255:
            raise ValueError("response.alive_if.value 必须在 00~FF 之间")
        if idx >= length:
            raise ValueError("response.alive_if.index 必须小于 request.length（%d）" % length)
        alive_if = (idx, val)
    else:
        raise ValueError("response.alive_if 必须是对象")
    try:
        timeout = float(rs.get("timeout", 1.2))
    except Exception:
        raise ValueError("response.timeout 必须是数字")
    if not 0.1 <= timeout <= 5.0:
        raise ValueError("response.timeout 必须在 0.1~5 秒之间")

    return Profile(name=name, kind=kind, vid=vid, pids=pids,
                   usage_page=usage_page, usage=usage, payload=payload,
                   length=length, report_size=length + 1, expect=expect,
                   level_index=level_index, charge_index=charge_index,
                   charge_on=tuple(charge_on), wired_if=dict(wired_if),
                   wired_pids=wired_pids, alive_if=alive_if,
                   read_timeout=timeout, file=os.path.basename(path))


PROFILES = []          # 已加载的配置（按文件名排序）
PROFILE_ERRORS = {}    # 文件名 -> 不合法原因（只写日志，界面不展示）


def reload_profiles() -> int:
    """扫描 device 目录，只把合法的配置放进 PROFILES。"""
    global PROFILES, PROFILE_ERRORS
    d = device_dir()
    profs, errors, names = [], {}, set()
    try:
        files = [f for f in os.listdir(d) if f.lower().endswith(".json")]
        # 按修改时间从早到晚处理：同名时保留早先那份，
        # 后来复制出来的副本会被判重跳过，不会把原文件顶掉
        files.sort(key=lambda f: (os.path.getmtime(os.path.join(d, f)), f))
    except Exception as e:
        log.warning("读取 device 目录失败: %s", e)
        files = []
    for fn in files:
        try:
            p = load_profile(os.path.join(d, fn))
        except Exception as e:
            errors[fn] = str(e)
            log.warning("配置不合法，已忽略 %s：%s", fn, e)
            continue
        if p.name in names:
            errors[fn] = "名称与其它配置重复"
            log.warning("配置名称重复，已忽略 %s：%s", fn, p.name)
            continue
        names.add(p.name)
        profs.append(p)
    PROFILES, PROFILE_ERRORS = profs, errors
    if profs:
        profs.sort(key=lambda p: p.file)     # 展示按文件名排，顺序稳定
        log.info("已加载 %d 个设备配置：%s", len(profs),
                 "、".join("%s(%s)" % (p.name, p.file) for p in profs))
    else:
        log.warning("device 目录里没有可用的配置文件：%s", d)
    return len(profs)


def match_profile(vid: int, pid: int, usage_page: int, usage: int):
    """按 VID / PID / 用法匹配已加载的配置，先到先得。"""
    for p in PROFILES:
        if p.vid != vid or p.usage_page != usage_page or p.usage != usage:
            continue
        if p.pids and pid not in p.pids:
            continue
        return p
    return None


# ---------------------------------------------------------------- 设备

@dataclass
class DeviceInfo:
    key: str
    kind: str            # mouse | keyboard | generic
    vid: int
    pid: int
    path: bytes
    product: str
    usage_page: int
    usage: int
    profile: Optional[Profile] = None


def scan_devices() -> list:
    """枚举所有能匹配到配置文件的设备；同一物理设备只取一个接口。"""
    found = {}
    try:
        for d in hid.enumerate():
            vid = int(d.get("vendor_id") or 0)
            pid = int(d.get("product_id") or 0)
            up = d.get("usage_page")
            u = d.get("usage")
            prof = match_profile(vid, pid, up, u)
            if prof is None:
                continue
            key = "%04x:%04x" % (vid, pid)
            if key in found:
                # 同型号插了两台（VID/PID 一样）：给后来的一台加序号，
                # 否则第二台会被当成重复直接丢掉
                n = 2
                while "%s#%d" % (key, n) in found:
                    n += 1
                key = "%s#%d" % (key, n)
            found[key] = DeviceInfo(
                key=key, kind=prof.kind, vid=vid, pid=pid,
                path=d["path"], product=d.get("product_string") or "",
                usage_page=up, usage=u, profile=prof,
            )
    except Exception as e:
        log.warning("枚举设备失败: %s", e)

    # 蓝牙输入设备：系统能给出电量就算有效，不看配置文件
    try:
        hid_names = {(d.product or "").strip().lower()
                     for d in found.values() if d.product}
        for it in bt_snapshot(blocking=True):   # 查询只有几毫秒，过期就顺手刷新
            if it["key"] in found:
                continue
            if it["name"].strip().lower() in hid_names:
                continue        # 同一个设备已经通过 USB / 2.4G 在监视了
            found[it["key"]] = DeviceInfo(
                key=it["key"], kind="bt", vid=0, pid=0, path=b"",
                product=it["name"], usage_page=0, usage=0, profile=None)
    except Exception as e:
        log.warning("枚举蓝牙设备失败: %s", e)

    return list(found.values())


@dataclass
class Reading:
    connected: bool = False
    level: Optional[int] = None
    charging: bool = False
    wired: bool = False
    error: str = ""


_DEV_LOCKS = {}
_DEV_LOCKS_GUARD = threading.Lock()


def device_io_lock(key: str) -> "threading.RLock":
    """同一台设备（按 key）共用一把锁。

    设备重建时新旧 BaseDevice 会短暂共存，各自持锁无法防止两者同时读写
    同一个物理接口，所以锁按 key 共享，直到进程结束。"""
    with _DEV_LOCKS_GUARD:
        lock = _DEV_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _DEV_LOCKS[key] = lock
        return lock


class BaseDevice:
    payload_len = 64
    report_size = 65

    def __init__(self, info: DeviceInfo, force_mode: str = "auto"):
        self.info = info
        self.force_mode = force_mode or "auto"
        self.dev = None
        self.retired = False
        self._evt = None
        self._thread = None      # 负责轮询本设备的线程
        self.fail = 0
        self.last_product = info.product or info.key
        # 同一个设备的请求/回包必须串行：后台轮询线程和「运行自检」
        # 会同时读同一台设备，交叉发送会让回包被错误的调用消费。
        # 用按 key 共享的锁，重建后的新对象也会和旧对象互斥。
        self._io_lock = device_io_lock(info.key)

    # --- 基本信息 ---
    @property
    def key(self) -> str:
        return self.info.key

    def name(self) -> str:
        return (self.last_product or self.info.product or self.info.key).strip()

    def mode_text(self) -> str:
        return PROFILE_KINDS.get(self.info.kind, "外设")

    # --- 打开 / 关闭 ---
    def open(self) -> bool:
        self.close()
        try:
            dev = hid.device()
            dev.open_path(self.info.path)
            dev.set_nonblocking(1)
            self.dev = dev
            return True
        except Exception as e:
            log.debug("[%s] open_path 失败: %s", self.key, e)

        # 路径失效（换 USB 口）时重新枚举一次
        for d in hid.enumerate(self.info.vid):
            if d.get("usage_page") == self.info.usage_page and d.get("usage") == self.info.usage:
                try:
                    dev = hid.device()
                    dev.open_path(d["path"])
                    dev.set_nonblocking(1)
                    self.dev = dev
                    self.info.path = d["path"]
                    log.info("[%s] 重新定位接口成功", self.key)
                    return True
                except Exception:
                    continue
        return False

    def close(self):
        with self._io_lock:      # 与读写互斥，别把别人正在用的句柄关掉
            if self.dev is not None:
                try:
                    self.dev.close()
                except Exception:
                    pass
            self.dev = None

    # --- 通信 ---
    def _write(self, payload: bytes):
        self.dev.write(b"\x00" + payload)

    def _read_match(self, match: Callable[[list], bool], timeout=1.2):
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self.dev.read(self.report_size, 60)
            if not data:
                time.sleep(0.005)   # 万一某个绑定立即返回空，也别空转烧 CPU
                continue
            if match(data):
                return data
        return None

    def poll(self) -> Reading:
        raise NotImplementedError

    def poll_forced(self) -> Reading:
        with self._io_lock:         # 与自检等其它调用方互斥
            rd = self.poll()
        if rd.connected:
            if self.force_mode == "wired":
                rd.wired = True
                rd.charging = True
            elif self.force_mode == "wireless":
                rd.wired = False
        return rd


class ProfileDevice(BaseDevice):
    """按 JSON 配置驱动的通用设备：发什么、怎么解析都由配置决定。"""

    def __init__(self, info: DeviceInfo, force_mode: str = "auto"):
        super().__init__(info, force_mode)
        self.p = info.profile
        self.report_size = self.p.report_size
        self.payload_len = self.p.length

    def name(self) -> str:
        return self.p.name

    def mode_text(self) -> str:
        return PROFILE_KINDS.get(self.p.kind, "外设")

    def _request(self):
        """发一次电量请求，等一条能匹配上的回包。"""
        p = self.p
        self._write(p.payload + b"\x00" * (p.length - len(p.payload)))
        need = max(p.level_index, p.charge_index, len(p.expect) - 1) + 1

        def ok(b):
            return len(b) >= need and bytes(b[:len(p.expect)]) == p.expect

        return self._read_match(ok, p.read_timeout)

    def _wired_hit(self, level: int, charge: int) -> bool:
        for k, v in (self.p.wired_if or {}).items():
            if (level if k == "level" else charge) != v:
                return False
        return True

    def poll(self) -> Reading:
        if self.dev is None and not self.open():
            return Reading(connected=False, error="未连接")
        p = self.p
        try:
            r = self._request()
            if r is None:
                r = self._request()      # 无线链路偶尔丢一包，再试一次
            if r is None:
                # 接收器还插着但设备关机/断了，这时不该报「已连接」
                return Reading(connected=False,
                               error="无响应（设备可能已关机）")

            if p.alive_if:
                idx, want = p.alive_if
                if len(r) <= idx or int(r[idx]) != want:
                    # 接收器把上一次的状态复述回来了，设备其实已经关机
                    return Reading(connected=False, error="设备可能已关机")

            level, charge = int(r[p.level_index]), int(r[p.charge_index])
            zero = (level == 0 and charge == 0)
            if p.wired_if and self._wired_hit(level, charge):
                wired_only = (not p.wired_pids) or (self.info.pid in p.wired_pids)
                if wired_only:
                    # 配置里写明了「这种回包代表纯供电」，没有电量可读
                    return Reading(connected=True, level=None,
                                   charging=True, wired=True)
                # 无线连接下也回这种包：多半是刚唤醒还没采到电量，重试一次
                r2 = self._request()
                if r2 is not None:
                    lv, ch = int(r2[p.level_index]), int(r2[p.charge_index])
                    if not self._wired_hit(lv, ch):
                        wired = ch in p.charge_on
                        return Reading(connected=True, level=min(100, lv),
                                       charging=wired, wired=wired)
                # 0/0 且重试还是 0/0：接收器收不到设备，按未连接处理，
                # 免得把「0%」当成真实电量去报警
                return Reading(connected=False, error="设备可能已关机")
            if zero:
                return Reading(connected=False, error="设备可能已关机")
            wired = charge in p.charge_on
            return Reading(connected=True, level=min(100, level),
                           charging=wired, wired=wired)
        except Exception as e:
            log.warning("[%s] 读取异常: %s", self.name(), e)
            self.close()
            return Reading(connected=False, error=str(e))


class BtDevice(BaseDevice):
    """蓝牙输入设备：电量取自系统属性，不需要配置文件，也不走 HID 报文。"""

    def open(self) -> bool:
        return True

    def close(self):
        pass

    def name(self) -> str:
        return (self.info.product or "蓝牙设备").strip()

    def mode_text(self) -> str:
        return "蓝牙"

    def poll(self) -> Reading:
        for it in bt_snapshot(blocking=True):
            if it["key"] == self.key:
                return Reading(connected=True, level=it["level"],
                               charging=False, wired=False)
        return Reading(connected=False, error="未连接")


def make_device(info: DeviceInfo, mode: str = "auto") -> BaseDevice:
    if info.profile is None:        # 蓝牙设备没有配置文件
        return BtDevice(info, mode)
    return ProfileDevice(info, mode)


# ---------------------------------------------------------------- 设置窗口

UI_BG = "#12161f"
UI_BG2 = "#0c1017"
UI_CARD = "#171d28"
UI_LINE = "#232c3a"
UI_FG = "#e8eef8"
UI_SUB = "#8b98ad"
UI_ACCENT = "#3ddc97"
UI_CHECK_BG = "#d7e0ee"     # 复选框底色：浅色才能看清系统绘制的勾


def apply_dark_titlebar(win) -> None:
    """让标题栏跟随深色主题（Win11 属性 20，Win10 退回 19）。"""
    try:
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        val = ctypes.c_int(1)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
        border = ctypes.c_int(0x2B2B2B)
        for attr in (34, 35):        # 边框颜色，否则四边还是白的
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(border), ctypes.sizeof(border))
    except Exception:
        pass


def show_error_dialog(win, msg: str) -> None:
    """Windows 错误提示框：红色叉图标 + 系统错误提示音。必须在 Tk 主线程调用。"""
    try:
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        mb = ctypes.windll.user32.MessageBoxW
        mb.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                       ctypes.c_wchar_p, ctypes.c_uint]
        mb.restype = ctypes.c_int
        mb(hwnd, msg, "错误", 0x10)          # MB_ICONERROR
    except Exception as e:
        log.warning("错误提示弹窗失败: %s", e)


class Tooltip:
    """轻量悬停提示，用于显示被省略的完整设备名。"""

    def __init__(self, widget, text_func):
        self.widget = widget
        self.text_func = text_func
        self.tip = None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def show(self, event=None):
        text = self.text_func()
        if not text or self.tip is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 10
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry("+%d+%d" % (x, y))
            self.tip.configure(bg=UI_LINE)
            tk.Label(self.tip, text=text, bg="#1e2634", fg=UI_FG,
                     font=("Microsoft YaHei UI", 8), padx=9, pady=6,
                     justify="left", anchor="w").pack(padx=1, pady=1)
        except Exception:
            self.tip = None

    def hide(self, event=None):
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


def elide(text: str, px: int, font) -> str:
    """按像素宽度截断，超出部分用省略号。"""
    if not text:
        return ""
    try:
        if font.measure(text) <= px:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid] + "…") <= px:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + "…"
    except Exception:
        return text if len(text) <= 18 else text[:17] + "…"


def setup_ttk_style(win) -> None:
    """统一的深色 ttk 主题；设置窗口和配置文件窗口共用。"""
    s = ttk.Style(win)
    try:
        s.theme_use("clam")
    except Exception:
        pass
    s.configure(".", background=UI_BG, foreground=UI_FG,
                fieldbackground=UI_BG2, bordercolor=UI_LINE,
                lightcolor=UI_BG, darkcolor=UI_BG, troughcolor=UI_BG2)
    s.configure("TFrame", background=UI_BG)
    s.configure("Card.TFrame", background=UI_CARD)
    s.configure("TLabel", background=UI_BG, foreground=UI_FG,
                font=("Microsoft YaHei UI", 9))
    s.configure("Card.TLabel", background=UI_CARD, foreground=UI_FG,
                font=("Microsoft YaHei UI", 9))
    s.configure("Sub.TLabel", background=UI_CARD, foreground=UI_SUB,
                font=("Microsoft YaHei UI", 8))
    s.configure("Head.TLabel", background=UI_BG, foreground=UI_SUB,
                font=("Microsoft YaHei UI", 8))
    s.configure("TCheckbutton", background=UI_CARD, foreground=UI_FG,
                font=("Microsoft YaHei UI", 9))
    s.map("TCheckbutton",
          background=[("active", UI_CARD)],
          foreground=[("disabled", UI_SUB)])
    s.configure("TButton", background="#1b2230", foreground=UI_FG,
                borderwidth=0, focusthickness=0, padding=(12, 7),
                font=("Microsoft YaHei UI", 9))
    s.map("TButton", background=[("active", "#242d3d"), ("disabled", "#151a24")])
    s.configure("Accent.TButton", background=UI_ACCENT, foreground="#04150e",
                borderwidth=0, padding=(14, 7),
                font=("Microsoft YaHei UI", 9, "bold"))
    s.map("Accent.TButton", background=[("active", "#4ee8a5")])
    s.configure("TSpinbox", arrowcolor=UI_FG, background=UI_BG2,
                fieldbackground=UI_BG2, foreground=UI_FG, borderwidth=0,
                arrowsize=12)
    s.configure("TCombobox", background=UI_BG2, fieldbackground=UI_BG2,
                foreground=UI_FG, arrowcolor=UI_FG, borderwidth=0,
                selectbackground=UI_BG2, selectforeground=UI_FG)
    s.configure("Thin.Vertical.TScrollbar", background="#2b3648",
                troughcolor=UI_CARD, bordercolor=UI_CARD,
                arrowcolor=UI_SUB, darkcolor="#2b3648", lightcolor="#2b3648",
                width=9, arrowsize=9)
    s.map("Thin.Vertical.TScrollbar",
          background=[("active", "#3a475d"), ("pressed", UI_ACCENT)])
    # readonly 状态下 clam 主题会回退成浅色底，必须显式覆盖
    s.map("TCombobox",
          fieldbackground=[("readonly", UI_BG2), ("!readonly", UI_BG2)],
          foreground=[("readonly", UI_FG), ("!readonly", UI_FG)],
          background=[("readonly", UI_BG2), ("active", "#242d3d")],
          selectbackground=[("readonly", UI_BG2)],
          selectforeground=[("readonly", UI_FG)],
          arrowcolor=[("readonly", UI_FG), ("active", UI_ACCENT)])
    win.option_add("*TCombobox*Listbox.background", UI_BG2)
    win.option_add("*TCombobox*Listbox.foreground", UI_FG)
    win.option_add("*TCombobox*Listbox.selectBackground", UI_ACCENT)
    win.option_add("*TCombobox*Listbox.selectForeground", "#04150e")


class SettingsDialog:
    """tkinter 设置窗口：勾选要监视的设备、调整提醒参数。"""

    INTERVALS = [(30, "30 秒"), (60, "1 分钟"), (120, "2 分钟"),
                 (300, "5 分钟"), (600, "10 分钟")]

    def __init__(self, app):
        self.app = app
        self.root = app.root
        self.win = tk.Toplevel(self.root)
        self.win.withdraw()          # 先藏起来，摆好位置再显示，避免闪一下再跳
        self.win.title("外设电量监视 · 设置")
        self.win.configure(bg=UI_BG)
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self.cancel)

        self._vars = {}
        self._list_frame = None
        self._tooltips = []
        self._rows = {}
        self._list_sig = None
        self._tick_id = None
        self._font9 = tkfont.Font(family="Microsoft YaHei UI", size=9)
        self.scale = self._dpi_scale()
        self._build_style()
        self._build()
        self._center(486, 668)
        self.refresh_device_list()

        # 窗口图标（默认是 Tk 的羽毛，换成自己的）
        self._icon_photo = None
        try:
            self._icon_photo = tk.PhotoImage(file=self.app.icon_png)
            self.win.iconphoto(True, self._icon_photo)
        except Exception as e:
            log.debug("设置窗口图标设置失败: %s", e)

        self._apply_dark_titlebar()

        self.win.deiconify()         # 位置已定，这时才真正显示
        self.win.transient()
        self.win.grab_set()
        self.win.focus_force()

        self._tick_id = self.win.after(1500, self._tick)

    def _apply_dark_titlebar(self):
        """让标题栏跟随深色主题，不然白条很突兀。"""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.win.winfo_id())
            if not hwnd:
                return
            val = ctypes.c_int(1)
            for attr in (20, 19):     # DWMWA_USE_IMMERSIVE_DARK_MODE：Win11 / Win10
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                    break
        except Exception as e:
            log.debug("深色标题栏设置失败: %s", e)

    def _tick(self):
        """定时刷新设备状态，断线/换接口能立刻反映出来。"""
        if not self.win.winfo_exists():
            return
        try:
            self.refresh_device_list()
        except Exception:
            log.debug("刷新设备列表失败", exc_info=True)
        self._tick_id = self.win.after(1500, self._tick)

    # --- 样式 ---
    def _build_style(self):
        setup_ttk_style(self.win)

    # --- 布局 ---
    def _section(self, parent, title):
        ttk.Label(parent, text=title, style="Head.TLabel").pack(
            anchor="w", pady=(self.px(14), self.px(6)))
        box = tk.Frame(parent, bg=UI_CARD, highlightbackground=UI_LINE,
                       highlightthickness=1, bd=0)
        box.pack(fill="x")
        inner = tk.Frame(box, bg=UI_CARD)
        inner.pack(fill="x", padx=self.px(12), pady=self.px(10))
        return inner

    def _toggle_row(self, parent, text, var, on_change=None, text_font=None):
        """自绘勾选行：☑ / ☐ 在深色背景上都清晰可见。"""
        row = tk.Frame(parent, bg=UI_CARD)
        row.pack(fill="x", pady=2)

        sym = tk.Label(row, text="", bg=UI_CARD, fg=UI_SUB, width=2,
                       font=("Segoe UI Symbol", 12), cursor="hand2")
        sym.pack(side="left")
        lbl = tk.Label(row, text=text, bg=UI_CARD, fg=UI_FG,
                       font=text_font or ("Microsoft YaHei UI", 9), cursor="hand2")
        lbl.pack(side="left")

        def sync(*_a):
            on = bool(var.get())
            sym.configure(text="☑" if on else "☐",
                          fg=UI_ACCENT if on else UI_SUB)

        def toggle(_e=None):
            var.set(not var.get())
            if on_change:
                on_change()

        sym.bind("<Button-1>", toggle)
        lbl.bind("<Button-1>", toggle)
        var.trace_add("write", lambda *a: sync())
        sync()
        return row, sym, lbl

    # --- 数字输入 / 阈值 ---
    def _spinbox(self, parent, var, lo, hi, step):
        """深色数字输入框：用经典 tk.Spinbox，
        因为 ttk 的输入框没法设 insertbackground，光标在深色底上看不见。"""
        return tk.Spinbox(
            parent, from_=lo, to=hi, increment=step, width=6,
            textvariable=var, bg=UI_BG2, fg=UI_FG,
            insertbackground=UI_FG, buttonbackground=UI_CARD,
            relief="flat", highlightthickness=1,
            highlightbackground=UI_LINE, highlightcolor=UI_ACCENT,
            disabledbackground=UI_BG2, justify="center",
            font=("Microsoft YaHei UI", 9))

    @staticmethod
    def _threshold_label(v: int) -> str:
        return ("%d%%" % v) if v in PRESET_THRESHOLDS else ("%d%%（自定义）" % v)

    def _sync_threshold_display(self):
        try:
            self._vars["threshold"].set(self._threshold_label(self._threshold_value))
        except Exception:
            pass

    def on_threshold_changed(self, value):
        """托盘右键改了阈值时，把界面上的显示同步过来。"""
        try:
            self._threshold_value = max(1, min(99, int(value)))
        except Exception:
            return
        self._sync_threshold_display()

    def _on_threshold_pick(self, _event=None):
        sel = self._vars["threshold"].get()
        if sel == CUSTOM_CHOICE:
            self._open_custom_threshold()
            return
        try:
            self._threshold_value = int(sel.rstrip("%"))
        except Exception:
            pass
        self._sync_threshold_display()

    @staticmethod
    def _parse_threshold(raw: str):
        """解析自定义阈值输入：('clear'|'ok'|'bad', 值)。"""
        raw = (raw or "").strip()
        if raw in ("", "0"):
            return "clear", None
        try:
            v = int(raw)
        except ValueError:
            return "bad", None
        if not 1 <= v <= 99:
            return "bad", None
        return "ok", v

    def _open_custom_threshold(self):
        """自定义阈值小窗口：保存后关闭；空或 0 表示清除自定义项。"""
        dlg = tk.Toplevel(self.win)
        dlg.title("自定义电量阈值")
        dlg.configure(bg=UI_BG)
        dlg.resizable(False, False)
        dlg.transient(self.win)
        w, h = self.px(320), self.px(190)
        self.win.update_idletasks()
        x = self.win.winfo_rootx() + (self.win.winfo_width() - w) // 2
        y = self.win.winfo_rooty() + (self.win.winfo_height() - h) // 3
        dlg.geometry("%dx%d+%d+%d" % (w, h, x, y))
        dlg.update_idletasks()
        apply_dark_titlebar(dlg)

        tk.Label(dlg, text="请输入 1 ~ 99 的整数（百分比）",
                 bg=UI_BG, fg=UI_FG,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=self.px(16),
                                                     pady=(self.px(14), self.px(6)))
        var = tk.StringVar(value=str(self._custom_value or ""))
        ent = tk.Entry(dlg, textvariable=var, width=6, justify="center",
                       bg=UI_BG2, fg=UI_FG, insertbackground=UI_FG, insertwidth=2,
                       relief="flat", highlightthickness=1,
                       highlightbackground=UI_LINE, highlightcolor=UI_ACCENT,
                       font=("Microsoft YaHei UI", 12))
        ent.pack(anchor="w", padx=self.px(16), ipady=self.px(4))
        tk.Label(dlg, text="留空或填 0 表示清除自定义阈值",
                 bg=UI_BG, fg=UI_SUB,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=self.px(16),
                                                     pady=(self.px(5), 0))

        def do_save(_e=None):
            state, _v = self._apply_custom_value(var.get())
            if state == "bad":
                show_error_dialog(dlg, "无效的阈值，请输入 1 ~ 99 的整数")
                var.set("")                 # 清空输入，让用户重填
                ent.focus_set()
                return
            dlg.destroy()

        bar = tk.Frame(dlg, bg=UI_BG)
        bar.pack(side="bottom", fill="x", padx=self.px(16), pady=self.px(12))
        ttk.Button(bar, text="保存", command=do_save).pack(side="right")
        ttk.Button(bar, text="取消", command=dlg.destroy).pack(
            side="right", padx=(0, self.px(8)))
        ent.bind("<Return>", do_save)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.grab_set()
        dlg.focus_force()
        ent.focus_set()
        ent.icursor("end")

    def _apply_custom_value(self, raw: str):
        """把输入应用到阈值状态；不合法时什么都不改。返回 (状态, 值)。"""
        state, v = self._parse_threshold(raw)
        if state == "bad":
            return state, None
        if state == "clear":
            self._custom_value = 0
            # 清掉自定义后，阈值回到默认值，不能还挂在原来的自定义值上
            self._threshold_value = int(DEFAULTS["low_threshold"])
        else:
            self._custom_value = 0 if v in PRESET_THRESHOLDS else v
            self._threshold_value = v
        self._sync_threshold_display()
        return state, v

    def _build_device_panel(self, parent):
        """设备独立面板：固定高度 + 垂直滚动条。"""
        panel = tk.Frame(parent, bg=UI_CARD, highlightbackground=UI_LINE,
                         highlightthickness=1, bd=0)
        # 让它吸收窗口的剩余高度，避免底部留一大片空白
        panel.pack(fill="both", expand=True)
        self._panel = panel

        head = tk.Frame(panel, bg=UI_CARD)
        head.pack(fill="x", padx=self.px(12), pady=(self.px(8), self.px(2)))
        tk.Label(head, text="监视的设备", bg=UI_CARD, fg=UI_SUB,
                 font=("Microsoft YaHei UI", 8)).pack(side="left")
        self._count_lbl = tk.Label(head, text="", bg=UI_CARD, fg=UI_SUB,
                                   font=("Microsoft YaHei UI", 8))
        self._count_lbl.pack(side="right")

        body = tk.Frame(panel, bg=UI_CARD)
        body.pack(fill="both", expand=True,
                  padx=(self.px(10), self.px(6)), pady=(0, self.px(8)))

        self._canvas = tk.Canvas(body, bg=UI_CARD, highlightthickness=0, bd=0,
                                 height=self.px(130))
        self._vsb = ttk.Scrollbar(body, orient="vertical", style="Thin.Vertical.TScrollbar",
                                  command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._vsb.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._vsb.pack(side="right", fill="y", padx=(4, 0))

        self._list_frame = tk.Frame(self._canvas, bg=UI_CARD)
        self._win = self._canvas.create_window((0, 0), window=self._list_frame, anchor="nw")
        self._list_frame.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfigure(self._win, width=e.width))

        panel.bind("<Enter>", lambda e: self.win.bind_all("<MouseWheel>", self._on_wheel))
        panel.bind("<Leave>", lambda e: self.win.unbind_all("<MouseWheel>"))

    def _on_wheel(self, event):
        try:
            first, last = self._canvas.yview()
            if first <= 0.0 and last >= 1.0:
                return          # 内容没超出，不需要滚
            self._canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass

    def _build(self):
        outer = tk.Frame(self.win, bg=UI_BG)
        outer.pack(fill="both", expand=True, padx=self.px(18), pady=(self.px(16), 0))

        self._build_device_panel(outer)

        bar = tk.Frame(outer, bg=UI_BG)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="重新扫描", command=self.rescan).pack(side="left")
        ttk.Button(bar, text="配置文件…", command=self.open_profiles).pack(
            side="left", padx=(8, 0))
        ttk.Label(bar, text="取消勾选即停止监视该设备",
                  style="Head.TLabel").pack(side="left", padx=10)

        # 提醒
        box = self._section(outer, "提醒")
        row1 = tk.Frame(box, bg=UI_CARD)
        row1.pack(fill="x", pady=3)
        ttk.Label(row1, text="低电量阈值", style="Card.TLabel").pack(side="left")

        self._threshold_value = int(self.app.cfg.get("low_threshold", 20))
        self._custom_value = int(self.app.cfg.get("custom_threshold", 0) or 0)
        self._vars["threshold"] = tk.StringVar()
        self._sync_threshold_display()
        self._threshold_cb = ttk.Combobox(
            row1, textvariable=self._vars["threshold"], width=14, state="readonly",
            values=[("%d%%" % v) for v in PRESET_THRESHOLDS] + [CUSTOM_CHOICE])
        self._threshold_cb.pack(side="right")
        self._threshold_cb.bind("<<ComboboxSelected>>", self._on_threshold_pick)

        row2 = tk.Frame(box, bg=UI_CARD)
        row2.pack(fill="x", pady=3)
        ttk.Label(row2, text="检查间隔", style="Card.TLabel").pack(side="left")
        self._vars["interval"] = tk.StringVar()
        cur = int(self.app.cfg.get("interval_sec", 60))
        label = dict(self.INTERVALS).get(cur, "1 分钟")
        self._vars["interval"].set(label)
        cb = ttk.Combobox(row2, textvariable=self._vars["interval"], width=10,
                          values=[l for _, l in self.INTERVALS], state="readonly")
        cb.pack(side="right")

        row3 = tk.Frame(box, bg=UI_CARD)
        row3.pack(fill="x", pady=3)
        ttk.Label(row3, text="重复提醒间隔", style="Card.TLabel").pack(side="left")
        self._vars["repeat"] = tk.IntVar(value=int(self.app.cfg.get("alert_repeat_min", 30)))
        ttk.Label(row3, text="分钟", style="Card.TLabel").pack(side="right")
        self._spinbox(row3, self._vars["repeat"], 5, 240, 5).pack(side="right", padx=(0, 6))

        # 启动
        box2 = self._section(outer, "启动")
        self._vars["autostart"] = tk.BooleanVar(value=bool(self.app.cfg.get("start_with_windows")))
        self._toggle_row(box2, "开机自动启动", self._vars["autostart"])
        self._vars["notify"] = tk.BooleanVar(value=bool(self.app.cfg.get("notify_on_connect", True)))
        self._toggle_row(box2, "设备接入时弹静音提示", self._vars["notify"])

        # 底部按钮
        footer = tk.Frame(self.win, bg=UI_BG)
        footer.pack(fill="x", side="bottom", padx=self.px(18), pady=self.px(16))
        # 三个按钮整体居中，不再左右分散
        box_btn = tk.Frame(footer, bg=UI_BG)
        box_btn.pack(anchor="center")
        ttk.Button(box_btn, text="恢复默认",
                   command=self.reset_defaults).pack(side="left", padx=(0, self.px(14)))
        ttk.Button(box_btn, text="取消",
                   command=self.cancel).pack(side="left", padx=(0, self.px(8)))
        ttk.Button(box_btn, text="保存", style="Accent.TButton",
                   command=self.save).pack(side="left")

    def _dpi_scale(self) -> float:
        """系统缩放比：96 DPI = 1.0，150% 缩放 = 1.5。
        注意不能用 winfo_fpixels('1i')，Tk 初始化前它固定返回 96。"""
        return max(1.0, system_dpi() / 96.0)

    def px(self, v: float) -> int:
        return int(round(v * self.scale))

    def _center(self, w, h):
        try:
            self.root.tk.call("tk", "scaling", system_dpi() / 72.0)
        except Exception:
            pass
        # 按缩放比放大窗口，视觉效果与不做 DPI 适配时一致，但字是清晰的
        w = self.px(w)
        h = self.px(h)
        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        x = max(0, int((sw - w) / 2))
        y = max(0, int((sh - h) / 2))       # 真正居中，不再往上偏
        self.win.geometry("%dx%d+%d+%d" % (w, h, x, y))

    # --- 设备列表 ---
    def refresh_device_list(self):
        """每 1.5 秒调一次：设备集合变化才重建行，否则只刷新状态文字。"""
        self.app.rescan_devices()
        infos = self.app.all_infos()
        sig = tuple(i.key for i in infos)
        if sig != self._list_sig:
            self._rebuild_rows(infos)
            self._list_sig = sig
        else:
            self._refresh_status()
        self._update_count()

    def _rebuild_rows(self, infos):
        for t in self._tooltips:
            t.hide()
        self._tooltips = []
        for w in self._list_frame.winfo_children():
            w.destroy()
        self._rows = {}

        # 先把旧勾选状态收走：设备不在了就不能再算进「已启用 X / Y」的计数
        old_vars = {k[4:]: v for k, v in self._vars.items() if k.startswith("dev:")}
        for k in [k for k in self._vars if k.startswith("dev:")]:
            self._vars.pop(k, None)

        if not infos:
            tk.Label(self._list_frame, text="未检测到兼容设备",
                     bg=UI_CARD, fg=UI_SUB,
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=12, padx=4)
            self._update_count()
            self._canvas.configure(scrollregion=(0, 0, 0, 0))
            self._sync_scrollbar()
            return

        for info in infos:
            st = self.app.cfg.get("device_settings", {}).get(info.key, {})
            var = old_vars.get(info.key)
            if var is None:
                var = tk.BooleanVar(value=bool(st.get("enabled", True)))
            self._vars["dev:" + info.key] = var

            row = tk.Frame(self._list_frame, bg=UI_CARD)
            row.pack(fill="x", pady=1)
            row.columnconfigure(1, weight=1)

            # ☑ / ☐ 自绘，深色主题下两种状态都清楚
            sym = tk.Label(row, text="☑" if var.get() else "☐", bg=UI_CARD,
                           fg=UI_ACCENT if var.get() else UI_SUB, width=2,
                           font=("Segoe UI Symbol", 12), cursor="hand2")
            sym.grid(row=0, column=0, sticky="w")

            name = info.product or info.key
            lbl = tk.Label(row, text=elide(name, self.px(292), self._font9), bg=UI_CARD,
                           fg=UI_FG, anchor="w", font=("Microsoft YaHei UI", 9),
                           cursor="hand2")
            lbl.grid(row=0, column=1, sticky="we", padx=(2, 6))

            sl = tk.Label(row, text=self.app.status_text(info.key), bg=UI_CARD,
                          fg=UI_SUB, anchor="e",
                          font=("Microsoft YaHei UI", 8), width=9)
            sl.grid(row=0, column=2, sticky="e")

            # 悬停提示动态取状态，断开后内容也会跟着变
            self._tooltips.append(Tooltip(
                lbl,
                lambda k=info.key, n=name, kind=info.kind:
                "%s\n%s · %s\n状态：%s" % (n, k, self._kind_text(kind), self.app.status_text(k))
            ))

            def toggle(_e=None, v=var, s=sym):
                v.set(not v.get())
                on = bool(v.get())
                s.configure(text="☑" if on else "☐",
                            fg=UI_ACCENT if on else UI_SUB)
                self._update_count()

            sym.bind("<Button-1>", toggle)
            lbl.bind("<Button-1>", toggle)
            sl.bind("<Button-1>", toggle)

            self._rows[info.key] = {"status": sl, "var": var, "sym": sym}

        self._list_frame.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._sync_scrollbar()

    def _refresh_status(self):
        for key, item in self._rows.items():
            try:
                item["status"].configure(text=self.app.status_text(key))
            except Exception:
                pass

    def _sync_scrollbar(self):
        """设备装得下就收起滚动条，多了再显示。"""
        try:
            need = self._list_frame.winfo_reqheight()
            # 面板会随窗口拉伸，按实际可视高度判断是否需要滚动条
            avail = self._canvas.winfo_height()
            if avail < 20:
                avail = self.px(130)
            # 用 winfo_manager 判断是否已 pack，未映射时 ismapped 不可靠
            if need > avail:
                if not self._vsb.winfo_manager():
                    self._vsb.pack(side="right", fill="y", padx=(4, 0))
            else:
                if self._vsb.winfo_manager():
                    self._vsb.pack_forget()
        except Exception:
            pass

    def _update_count(self):
        total = 0
        enabled = 0
        for k, v in self._vars.items():
            if k.startswith("dev:"):
                total += 1
                if bool(v.get()):
                    enabled += 1
        self._count_lbl.configure(text="已启用 %d / %d" % (enabled, total))

    @staticmethod
    def _kind_text(kind):
        return PROFILE_KINDS.get(kind, "外设")

    def rescan(self):
        self.refresh_device_list()

    def open_profiles(self):
        d = self.app.profiles_dialog
        if d is not None:
            try:
                d.win.deiconify()
                d.win.lift()
                d.win.focus_force()
                return
            except Exception:
                self.app.profiles_dialog = None
        try:
            ProfilesDialog(self)
        except Exception:
            log.exception("打开配置文件窗口失败")

    # --- 动作 ---
    def reset_defaults(self):
        self._threshold_value = int(DEFAULTS["low_threshold"])
        self._custom_value = int(DEFAULTS.get("custom_threshold", 0))
        self._sync_threshold_display()
        self._vars["interval"].set("1 分钟")
        self._vars["repeat"].set(DEFAULTS["alert_repeat_min"])
        self._vars["autostart"].set(DEFAULTS["start_with_windows"])
        self._vars["notify"].set(DEFAULTS["notify_on_connect"])
        for k, v in self._vars.items():
            if k.startswith("dev:"):
                v.set(True)
        # 设备行的勾是自绘的，重置后需要重建才能刷新符号
        self._list_sig = None
        self.refresh_device_list()

    def _validate_inputs(self) -> bool:
        """校验可手输的字段；不合法就弹窗并把输入框还原成当前配置。True 才能保存。"""
        try:
            v = int(str(self._vars["repeat"].get()).strip())
        except Exception:
            v = None
        if v is None or not 5 <= v <= 240:
            show_error_dialog(self.win, "无效的重复提醒间隔，请输入 5 ~ 240 的整数")
            self._vars["repeat"].set(int(self.app.cfg.get("alert_repeat_min", 30)))
            return False
        return True

    def save(self):
        # 先校验可输入的字段，不合法就弹错误并把输入框还原，窗口保持开着
        if not self._validate_inputs():
            return
        cfg = self.app.cfg
        try:
            cfg["low_threshold"] = max(1, min(99, int(self._threshold_value)))
            cv = int(self._custom_value or 0)
            cfg["custom_threshold"] = (
                cv if (1 <= cv <= 99 and cv not in PRESET_THRESHOLDS) else 0)
            cfg["alert_repeat_min"] = max(5, min(240, int(self._vars["repeat"].get())))
        except Exception as e:
            log.warning("保存阈值/间隔失败: %s", e)
        label = self._vars["interval"].get()
        for sec, text in self.INTERVALS:
            if text == label:
                cfg["interval_sec"] = sec
        cfg["notify_on_connect"] = bool(self._vars["notify"].get())

        want_auto = bool(self._vars["autostart"].get())
        if want_auto != bool(cfg.get("start_with_windows")):
            cfg["start_with_windows"] = want_auto
            apply_autostart(want_auto)

        settings = cfg.setdefault("device_settings", {})
        for k, v in self._vars.items():
            if not k.startswith("dev:"):
                continue
            key = k[4:]
            item = settings.setdefault(key, {"enabled": True, "mode": "auto"})
            item["enabled"] = bool(v.get())

        if not save_config(cfg):
            # 程序目录不可写（例如放在 Program Files）：明确告诉用户，别让它静默失败
            show_error_dialog(
                self.win,
                "设置保存失败：程序目录可能没有写入权限。\n"
                "请把程序放到用户目录或桌面后重试。")
        self.app.on_config_changed()
        log.info("设置已保存: 阈值=%s%% 间隔=%ss 设备=%d 项",
                 cfg["low_threshold"], cfg["interval_sec"], len(settings))
        self.close()

    def cancel(self):
        self.close()

    def close(self):
        # 从设置里打开的配置文件窗口跟着一起关，避免留下孤儿窗口
        if self.app.profiles_dialog is not None:
            try:
                self.app.profiles_dialog.close(from_settings=True)
            except Exception:
                pass
        if self._tick_id:
            try:
                self.win.after_cancel(self._tick_id)
            except Exception:
                pass
            self._tick_id = None
        for t in self._tooltips:
            t.hide()
        self._tooltips = []
        try:
            self.win.unbind_all("<MouseWheel>")
        except Exception:
            pass
        try:
            self.win.grab_release()
        except Exception:
            pass
        self.win.destroy()
        self.app.settings_dialog = None



class WelcomeDialog:
    """首次使用的欢迎引导（不写注册表：只在 config.json 不存在时出现）。"""

    BODY = """欢迎使用外设电量监视！

本程序用于读取无线外设（鼠标、键盘、蓝牙设备）的剩余电量，常驻系统托盘运行。

【基础使用】
1. 程序启动后自动扫描已支持的外设；
2. 鼠标右键点击托盘图标打开菜单，可设置低电量提醒阈值；
3. 左键单击托盘图标打开设置窗口，可以启用/禁用设备、修改提醒、设置开机自启。

【添加设备配置文件】
如果你的设备暂未被支持，可以添加自定义设备配置：
1. 在程序目录打开配置文件夹（device）；
2. 将设备 json 配置放入该目录；
3. 在托盘菜单打开「配置文件…」窗口，点击刷新即可加载。

【提示】
· 程序为便携版本，可以直接复制整个文件夹移动使用；
· 读不到电量可以查看 app.log 日志（和 PeriphBattery.exe 在同一文件夹），
  通过 GitHub Issue 反馈问题；
· 你可以点击下方按钮直接管理设备配置文件。

点击【完成】关闭此引导窗口，程序就可以正常使用。"""

    def __init__(self, app_ref):
        self.app = app_ref
        self.scale = max(1.0, system_dpi() / 96.0)
        self._font_title = tkfont.Font(family="Microsoft YaHei UI", size=14, weight="bold")
        self._font_body = tkfont.Font(family="Microsoft YaHei UI", size=9)
        self.win = tk.Toplevel(app_ref.root)
        self.win.withdraw()
        self.win.title("欢迎使用 " + APP_NAME)
        self.win.configure(bg=UI_BG)
        self.win.resizable(False, False)
        setup_ttk_style(self.win)
        self._build()
        self._fit_and_center()
        try:
            apply_dark_titlebar(self.win)
        except Exception:
            pass
        # 窗口图标：不设的话标题栏和任务栏都是 Tk 默认的羽毛
        self._icon_photo = None
        try:
            self._icon_photo = tk.PhotoImage(file=self.app.icon_png)
            self.win.iconphoto(True, self._icon_photo)
        except Exception as e:      # noqa: BLE001
            log.debug("引导窗口图标设置失败: %s", e)
        self.win.protocol("WM_DELETE_WINDOW", self.finish)
        self.win.deiconify()
        try:
            self.win.transient()
            self.win.grab_set()
        except Exception:
            pass
        self._raise_to_front()
        log.info("首次使用：欢迎引导已显示")

    def _raise_to_front(self):
        """开机自启时桌面上可能已经开着别的窗口，把引导窗口抬到最前。

        这是个模态窗口（grab_set 之后别的窗口本来也点不动），所以全程置顶，
        关闭时一起收掉，避免"弹在后台看不见"。"""
        try:
            self.win.lift()
            self.win.attributes("-topmost", True)
            self.win.focus_force()
            self.win.after(200, self.win.focus_force)
        except Exception as e:      # noqa: BLE001
            log.debug("引导窗口置顶失败: %s", e)

    def px(self, v: float) -> int:
        return int(round(v * self.scale))

    def _build(self):
        pad = self.px(26)
        outer = tk.Frame(self.win, bg=UI_BG)
        outer.pack(fill="both", expand=True, padx=pad, pady=(pad, self.px(6)))

        tk.Label(outer, text="欢迎使用 " + APP_NAME + "！", bg=UI_BG, fg=UI_FG,
                 font=self._font_title, anchor="center", justify="center").pack(fill="x")
        tk.Frame(outer, bg=UI_LINE, height=max(1, self.px(1))).pack(
            fill="x", pady=(self.px(12), self.px(14)))

        # 用 Label + wraplength 排版：高度随内容自适应，不需要固定 Text 行数
        body = tk.Label(outer, text=self.BODY, bg=UI_BG, fg=UI_SUB,
                        font=self._font_body, justify="left", anchor="w",
                        wraplength=self.px(520))
        body.pack(fill="x")

        bar = tk.Frame(self.win, bg=UI_BG)
        bar.pack(fill="x", padx=pad, pady=(self.px(4), pad))
        # 两个按钮作为一组整体居中（内层 frame 按内容宽度居中）
        inner = tk.Frame(bar, bg=UI_BG)
        inner.pack(anchor="center")
        ttk.Button(inner, text="打开配置文件管理",
                   command=self.open_profiles).pack(side="left")
        ttk.Button(inner, text="完成", command=self.finish).pack(
            side="left", padx=(self.px(12), 0))

    def _fit_and_center(self):
        """按内容算出窗口大小（DPI 已缩放），再放到屏幕正中。"""
        try:
            self.win.tk.call("tk", "scaling", system_dpi() / 72.0)
        except Exception:
            pass
        try:
            self.win.update_idletasks()
            w = self.win.winfo_reqwidth() + self.px(2)
            h = self.win.winfo_reqheight() + self.px(2)
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
            x = max(0, int((sw - w) / 2))
            y = max(0, int((sh - h) / 2))
            self.win.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except Exception as e:      # noqa: BLE001
            log.debug("欢迎引导窗口定位失败: %s", e)

    def open_profiles(self):
        """以独立非模态窗口打开配置管理，引导窗口保持打开。"""
        try:
            self.win.grab_release()      # 临时让出模态，否则配置窗口点不动
        except Exception:
            pass
        try:
            self.app.open_profiles_window()   # 内部是 root.after(0, ...)，窗口不会立刻存在
            self._attach_profiles()
            log.info("欢迎引导：已请求打开配置文件管理窗口")
        except Exception:
            log.exception("欢迎引导：打开配置文件管理失败")
            self._regrab()

    def _attach_profiles(self, tries: int = 0):
        """等配置窗口真正建出来再挂关闭事件；一直没建出来就恢复模态。"""
        dlg = getattr(self.app, "profiles_dialog", None)
        if dlg is not None and dlg.win.winfo_exists():
            win = dlg.win
            win.bind("<Destroy>", lambda e, w=win: self._on_profiles_closed(e, w), add="+")
            return
        if tries < 20:
            try:
                self.win.after(50, lambda: self._attach_profiles(tries + 1))
            except Exception:
                pass
        else:
            log.warning("欢迎引导：配置窗口未能打开，恢复模态")
            self._regrab()

    def _on_profiles_closed(self, event, win):
        if event.widget is not win:
            return
        self._regrab()

    def _regrab(self):
        try:
            if self.win.winfo_exists():
                self.win.grab_set()      # 配置窗口关掉后恢复引导窗口的模态
        except Exception:
            pass

    def finish(self):
        try:
            self.win.grab_release()
            self.win.attributes("-topmost", False)
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass
        self.app.welcome_dialog = None
        log.info("首次使用引导已完成")


class ProfilesDialog:
    """已加载的设备配置文件（独立窗口，列表可滚动）。

    parent 为空表示从托盘直接打开（没有设置窗口）。"""

    def __init__(self, parent: Optional[SettingsDialog] = None, app_ref=None):
        self.parent = parent
        self.app = app_ref or (parent.app if parent else None)
        self.scale = parent.scale if parent else max(1.0, system_dpi() / 96.0)
        master = parent.win if parent else self.app.root
        self.win = tk.Toplevel(master)
        self.win.withdraw()
        self.win.title("已加载的配置文件")
        self.win.configure(bg=UI_BG)
        self.win.resizable(False, False)
        setup_ttk_style(self.win)      # 独立打开时也要有深色按钮/滚动条
        self._build()
        self._center(560, 440)
        try:
            photo = parent._icon_photo if parent else tk.PhotoImage(file=self.app.icon_png)
            self._icon_photo = photo
            self.win.iconphoto(True, photo)
        except Exception:
            self._icon_photo = None
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        # 乘早一次 + 每次映射后补一次：窗口刚创建时 DWM 可能还没准备好
        self._apply_dark_titlebar()
        self.win.bind("<Map>", lambda e: self._apply_dark_titlebar(), add="+")
        if parent:
            self.win.transient(parent.win)
            self.win.grab_set()
        if self.app is not None:
            self.app.profiles_dialog = self
        self.win.deiconify()
        self.win.focus_force()
        self.render()

    def px(self, v: float) -> int:
        return int(round(v * self.scale))

    def _center(self, w, h):
        w, h = self.px(w), self.px(h)
        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        x = max(0, int((sw - w) / 2))
        y = max(0, int((sh - h) / 2))
        self.win.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.win.update_idletasks()

    def _apply_dark_titlebar(self):
        try:
            hwnd = ctypes.windll.user32.GetParent(self.win.winfo_id())
            if not hwnd:
                hwnd = self.win.winfo_id()
            val = ctypes.c_int(1)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                    break
            # 边框跟着一起变深，否则四边还是白的
            border = ctypes.c_int(0x2B2B2B)
            for attr in (34, 35):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(border), ctypes.sizeof(border))
        except Exception as e:
            log.debug("配置窗口深色标题栏失败: %s", e)

    def _build(self):
        outer = tk.Frame(self.win, bg=UI_BG)
        outer.pack(fill="both", expand=True,
                   padx=self.px(16), pady=(self.px(14), self.px(14)))

        self._title = tk.Label(outer, text="", bg=UI_BG, fg=UI_FG,
                               font=("Microsoft YaHei UI", 10, "bold"))
        self._title.pack(anchor="w")
        tk.Label(outer, text=device_dir(), bg=UI_BG, fg=UI_SUB,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(2, self.px(8)))

        body = tk.Frame(outer, bg=UI_CARD, highlightbackground=UI_LINE,
                        highlightthickness=1, bd=0)
        body.pack(fill="both", expand=True)
        self._canvas = tk.Canvas(body, bg=UI_CARD, highlightthickness=0, bd=0)
        self._vsb = ttk.Scrollbar(body, orient="vertical",
                                  style="Thin.Vertical.TScrollbar",
                                  command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._vsb.set)
        self._canvas.pack(side="left", fill="both", expand=True,
                          padx=(self.px(8), self.px(4)), pady=self.px(8))
        self._vsb.pack(side="right", fill="y", padx=(0, self.px(6)),
                       pady=self.px(8))          # 常驻滚动条
        self._list = tk.Frame(self._canvas, bg=UI_CARD)
        self._win = self._canvas.create_window((0, 0), window=self._list, anchor="nw")
        self._list.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfigure(
            self._win, width=e.width))
        self._canvas.bind("<Enter>", lambda e: self.win.bind_all(
            "<MouseWheel>", self._on_wheel))
        self._canvas.bind("<Leave>", lambda e: self.win.unbind_all("<MouseWheel>"))

        # 两行按钮：常用操作一行，辅助操作一行，避免单行过挤
        bar = tk.Frame(outer, bg=UI_BG)
        bar.pack(fill="x", pady=(self.px(10), 0))
        ttk.Button(bar, text="添加配置文件…",
                   command=self.add_profile).pack(side="left")
        ttk.Button(bar, text="打开 配置文件夹",
                   command=self.open_folder).pack(side="left", padx=self.px(8))
        ttk.Button(bar, text="刷新", command=self.refresh).pack(side="left")

        bar2 = tk.Frame(outer, bg=UI_BG)
        bar2.pack(fill="x", pady=(self.px(8), 0))
        ttk.Button(bar2, text="查看AI设备配置提示词",
                   command=self.open_ai_prompt).pack(side="left")
        ttk.Button(bar2, text="关闭", command=self.close).pack(side="right")

    def open_ai_prompt(self):
        """打开随身携带的「AI 提示词」，帮用户生成自己的设备配置。"""
        path = os.path.join(app_dir(), AI_PROMPT_NAME)
        if not os.path.isfile(path):
            log.warning("%s 文件不存在：%s", AI_PROMPT_NAME, path)
            show_error_dialog(self.win,
                              "未找到 AI 设备配置提示词文件，请重新下载完整便携包。")
            return
        try:
            os.startfile(path)
            log.info("打开AI设备配置提示词文档：%s", path)
        except Exception as e:      # noqa: BLE001
            log.warning("打开AI设备配置提示词失败: %s", e)
            show_error_dialog(self.win, "打开失败：%s" % e)

    def _on_wheel(self, event):
        try:
            first, last = self._canvas.yview()
            if first <= 0.0 and last >= 1.0:
                return
            self._canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass

    def render(self):
        for w in self._list.winfo_children():
            w.destroy()
        profs = list(PROFILES)
        self._title.configure(text="已加载 %d 个配置文件" % len(profs))
        if not profs:
            tk.Label(self._list, text="配置文件夹里还没有可用的配置文件",
                     bg=UI_CARD, fg=UI_SUB,
                     font=("Microsoft YaHei UI", 9)).pack(
                anchor="w", padx=self.px(6), pady=self.px(10))
            return
        for p in profs:
            row = tk.Frame(self._list, bg=UI_CARD)
            row.pack(fill="x", padx=self.px(6), pady=(self.px(5), 0))
            tk.Label(row, text=p.name, bg=UI_CARD, fg=UI_FG, anchor="w",
                     font=("Microsoft YaHei UI", 9, "bold")).pack(fill="x")
            tk.Label(row, text="%s · %s · %s" % (
                         p.file, PROFILE_KINDS.get(p.kind, "外设"), p.match_text()),
                     bg=UI_CARD, fg=UI_SUB, anchor="w",
                     font=("Microsoft YaHei UI", 8)).pack(fill="x")
            tk.Frame(self._list, bg=UI_LINE, height=1).pack(
                fill="x", padx=self.px(6), pady=(self.px(4), 0))

    def refresh(self):
        """重新读取 device 目录、重建设备表；有新设备就发通知。"""
        try:
            new = self.app.reload_devices()
        except Exception:
            log.exception("刷新设备配置失败")
            new = []
        self.render()
        if self.parent is not None:      # 从托盘单独打开时没有设置窗口
            try:
                self.parent.refresh_device_list()
            except Exception:
                log.debug("刷新设置窗口设备列表失败", exc_info=True)
        if new:
            threading.Thread(target=self.app.notify_added, args=(new,),
                             daemon=True).start()

    # --- 添加配置文件 ---
    def add_profile(self):
        """选一个 json 文件加进 device 目录（先校验，再复制）。"""
        try:
            from tkinter import filedialog
            path = filedialog.askopenfilename(
                parent=self.win, title="选择设备配置文件",
                filetypes=[("设备配置 (*.json)", "*.json"), ("所有文件", "*.*")])
        except Exception:
            log.exception("打开文件选择框失败")
            return
        if path:
            self._add_profile_path(path)

    def _add_profile_path(self, path: str) -> bool:
        """校验并复制一个配置文件；不合法就提示错误，合法则刷新列表。"""
        try:
            prof = load_profile(path)
        except Exception as e:
            log.warning("配置文件不合法，拒绝添加: %s（%s）", path, e)
            self._error("无效的配置文件")
            return False

        d = device_dir()
        for q in PROFILES:      # 同名配置会被忽略，等于没加上，直接判为无效
            same_file = (os.path.abspath(os.path.join(d, q.file))
                         == os.path.abspath(path))
            if q.name == prof.name and not same_file:
                log.warning("配置文件名称重复，拒绝添加: %s（%s）", path, prof.name)
                self._error("无效的配置文件")
                return False

        base = os.path.basename(path)
        stem, ext = os.path.splitext(base)
        dst = os.path.join(d, base)
        n = 2
        while os.path.exists(dst) and os.path.abspath(dst) != os.path.abspath(path):
            dst = os.path.join(d, "%s-%d%s" % (stem, n, ext))
            n += 1
        try:
            if os.path.abspath(dst) != os.path.abspath(path):
                shutil.copyfile(path, dst)
        except Exception as e:
            log.warning("复制配置文件失败: %s（%s）", path, e)
            self._error("无法写入配置文件夹")
            return False

        log.info("已添加配置文件: %s → %s", path, dst)
        self.refresh()
        return True

    def _error(self, msg: str):
        # 统一回 Tk 主线程弹窗，避免后台线程调用时报
        # "main thread is not in main loop"
        try:
            self.win.after(0, lambda: show_error_dialog(self.win, msg))
        except Exception:
            show_error_dialog(self.win, msg)

    def open_folder(self):
        try:
            os.startfile(device_dir())
            log.info("已打开 device 目录")
        except Exception as e:
            log.warning("打开 device 目录失败: %s", e)

    def close(self, from_settings: bool = False):
        try:
            self.win.unbind_all("<MouseWheel>")
        except Exception:
            pass
        try:
            self.win.grab_release()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass
        if self.app is not None and self.app.profiles_dialog is self:
            self.app.profiles_dialog = None
        if self.parent is not None and not from_settings:
            try:
                self.parent.win.grab_set()
            except Exception:
                pass


# ---------------------------------------------------------------- 托盘主程序

class App:
    def __init__(self):
        self.cfg = CFG
        self.root = None
        self.icon = None
        self.settings_dialog = None
        self.profiles_dialog = None

        self._all_infos = []
        self._devices = []
        self.readings = {}          # key -> Reading
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()

        self.alert_sent = {}
        self.connect_sent = {}
        self.boot_time = time.time()
        self.refresh_until = 0.0
        self._last_sig = None
        self._need_rescan = False    # 由 worker 置位，ui_loop 统一执行重建
        self._last_scan = 0.0        # 上次枚举设备表的时间
        self._last_title_sync = 0.0  # 上次写 tooltip 文本的时间
        self._rebuild_lock = threading.RLock()
        self.autostart = AUTOSTART_FLAG in sys.argv   # 由开机自启拉起
        self._flash_until = 0.0      # 自检后短时闪烁的结束时间
        self._need_menu_refresh = False   # 阈值变化后重建托盘菜单
        self.mute_alerts = False     # 临时停用低电量通知（只在本次运行有效）
        self._loop_err_ts = 0.0      # 主循环异常日志的限频时间戳
        self._save_warned = False    # 配置保存失败只提示一次
        self.notify_ok = True        # 通知权限是否可用（启动时读注册表）
        self.notify_notes = []
        self._last_notify_check = 0.0
        try:
            # 先用注册表给个初判（几十微秒），真正的结论由 ui_loop
            # 里的系统 API 查询给出，避免拖慢启动
            self.notify_ok, self.notify_notes = diagnose_notifications()
        except Exception as e:
            log.warning("通知权限检查失败: %s", e)
        try:
            reload_profiles()       # 读取 device/*.json
        except Exception as e:
            log.warning("加载设备配置失败: %s", e)
        try:
            bt_snapshot(blocking=True)   # 预热蓝牙电量（毫秒级），启动提示才不漏设备
        except Exception as e:
            log.warning("蓝牙设备预热失败: %s", e)
        _cleanup_legacy_toasts()

        self.alert_png = save_png(icon_alarm(TOAST_ICON), "alert.png")
        self.connect_png = save_png(icon_connected(TOAST_ICON), "connected.png")
        self.icon_png = save_png(icon_wired(TOAST_ICON), "app_icon.png")
        self.logo_png = app_logo_png()      # 横幅左上角（AUMID 图标）固定用软件图标
        set_app_logo(self.logo_png)
        self._root_icon = None
        register_aumid(self.logo_png)

    # --- 设备表 ---
    def rescan_devices(self):
        self._all_infos = scan_devices()
        return self._all_infos

    def reload_devices(self):
        """重新读取 device 目录并重建设备表，返回这次新增的设备。"""
        with self._rebuild_lock:        # 与后台重建互斥，避免两边同时动设备表
            old_keys = {d.key for d in self._devices}
            for dev in list(self._devices):
                dev.retired = True
                if dev._evt:
                    dev._evt.set()
            self._devices = []
            with self.lock:
                self.readings.clear()
            reload_profiles()
            self.rebuild_devices(rescan=True)
            new = [d for d in self._devices if d.key not in old_keys]
            self.apply(force=True)
            if new:
                log.info("发现新增设备: %s", "、".join(d.name() for d in new))
            return new

    def notify_added(self, devs):
        """有新设备接入监视时提示一次，内容格式与启动提示一致。"""
        if not devs or not self.notify_ok:
            return
        # 刚建好的设备还没读数，等第一轮回来再汇报（最多等 3 秒）
        deadline = time.time() + 3.0
        while time.time() < deadline and not self.stop_evt.is_set():
            rs = self.snapshot()
            if any(rs.get(d.key) and rs[d.key].connected for d in devs):
                break
            time.sleep(0.2)
        lines, _ = self._device_summary_lines(devices=devs)
        if not lines:
            return
        show_toast("添加设备", "\n".join(lines), self.connect_png, silent=True)
        log.info("新增设备提示: %s", " / ".join(lines))

    def all_infos(self):
        return self._all_infos

    def status_text(self, key: str) -> str:
        # 只有被明确取消勾选的设备才叫「未监视」；
        # 勾选着但还没开始轮询的，是「准备中」，不能显示成未监视
        st = self.cfg.get("device_settings", {}).get(key, {})
        if not st.get("enabled", True):
            return "未监视"
        with self.lock:
            rd = self.readings.get(key)
        if rd is None:
            return "准备中"
        if not rd.connected:
            return "未连接"
        if rd.wired:
            return "USB 供电"
        if rd.level is not None:
            return "%d%%" % rd.level
        return "已连接"

    def rebuild_devices(self, rescan: bool = True):
        with self._rebuild_lock:
            self._rebuild_devices_locked(rescan)

    def _rebuild_devices_locked(self, rescan: bool):
        if rescan:
            self.rescan_devices()
        wanted = {}
        for info in self._all_infos:
            st = self.cfg.get("device_settings", {}).get(info.key, {})
            if st.get("enabled", True):
                wanted[info.key] = (info, st.get("mode", "auto"))

        for dev in list(self._devices):
            if dev.key not in wanted:
                dev.retired = True
                if dev._evt:
                    dev._evt.set()
                # 句柄交给工作线程退出时自己关，避免在读写中途被关掉

        keep = [d for d in self._devices if not d.retired]
        existing = {d.key: d for d in keep}
        for key, (info, mode) in wanted.items():
            dev = existing.get(key)
            if dev is not None:
                if dev.force_mode == mode:
                    t = getattr(dev, "_thread", None)
                    if t is not None and not t.is_alive():
                        # 线程意外退出（异常、系统休眠等）→ 重新拉起，否则这台设备
                        # 会一直停在旧读数上，不会再刷新
                        log.warning("[%s] 轮询线程已退出，重新启动", dev.name())
                        self._start_worker(dev)
                    continue
                # 连接方式（auto/wired/wireless）只在构造时生效，
                # 配置改了必须重建，否则旧对象还按老方式解析
                dev.retired = True
                if dev._evt:
                    dev._evt.set()
                keep.remove(dev)
                log.info("[%s] 连接方式改为 %s，重建监视", dev.name(), mode)
            dev = make_device(info, mode)
            keep.append(dev)
            self._start_worker(dev)
            log.info("开始监视 %s (%s)", dev.name(), key)
        prev_count = len(self._devices)
        self._devices = keep
        if len(keep) != prev_count:
            self._refresh_menu()

        with self.lock:
            valid = {d.key for d in keep}
            for k in list(self.readings):
                if k not in valid:
                    self.readings.pop(k, None)

    # --- 轮询 ---
    def _start_worker(self, dev: BaseDevice):
        evt = threading.Event()
        dev._evt = evt
        t = threading.Thread(target=self._worker, args=(dev, evt), daemon=True)
        dev._thread = t
        t.start()

    def _worker(self, dev: BaseDevice, evt: threading.Event):
        while not self.stop_evt.is_set() and not dev.retired:
            try:
                self._poll_once(dev, evt)
            except Exception:
                # 关键：整轮都要兜住。异常一旦冒出去，这个线程就没了，
                # 设备会永远停在旧状态（开机启动时最容易踩到）。
                log.exception("[%s] 轮询循环异常，1 秒后继续", dev.name())
                time.sleep(1.0)

        # 句柄由这里关：此时本线程不再读它，不会和别的线程抢同一个设备
        dev.close()
        log.info("[%s] 轮询线程退出", dev.name())

    def _poll_once(self, dev: BaseDevice, evt: threading.Event):
        try:
            rd = dev.poll_forced()
        except Exception as e:
            log.exception("轮询异常 %s", e)
            rd = Reading(connected=False, error=str(e))

        with self.lock:
            prev = self.readings.get(dev.key) or Reading(connected=False)

        if rd.connected:
            dev.fail = 0
        else:
            dev.fail += 1
            # 设备关机/接收器收不到设备是明确信号，别拿旧电量糊弄；
            # 只有读取异常这种偶发失败才短暂保留上一轮数值
            soft = bool(rd.error) and ("无响应" in rd.error or "关机" in rd.error)
            if (not soft and dev.fail < 2 and prev.connected
                    and prev.level is not None):
                rd = Reading(connected=True, level=prev.level, charging=prev.charging,
                             wired=prev.wired, error="连接不稳定")
            # 连续失败到一定次数：可能是换了连接方式（PID 变了），
            # 旧 path 永远打不开，需要重新枚举一次设备表
            elif dev.fail >= 4 and dev.fail % 4 == 0:
                self._need_rescan = True
                log.info("[%s] 连续读取失败，触发设备表重新扫描", dev.name())

        with self.lock:
            self.readings[dev.key] = rd

        if (prev.level != rd.level or prev.connected != rd.connected
                or prev.wired != rd.wired):
            log.info("[%s] conn=%s wired=%s level=%s charge=%s %s",
                     dev.name(), rd.connected, rd.wired, rd.level, rd.charging, rd.error)

        if rd.connected and not prev.connected:
            self._on_connected(dev, rd)

        self._maybe_alert(dev, rd)

        interval = self.cfg.get("interval_sec", 60)
        # 先等再清：等的时候被 set（立即刷新）会马上返回，随后照旧
        # 走下一轮 poll，所以这里 clear 不会吞掉刷新请求
        evt.wait(interval if rd.connected else min(interval, 5))
        evt.clear()

    def refresh_now(self):
        if not self._devices:       # 没有在监视的设备，刷新没有意义
            return
        self.refresh_until = time.time() + 2.0
        for d in list(self._devices):
            if d._evt:
                d._evt.set()
        self.apply(force=True)

    def _refresh_menu(self):
        """设备增删后重建菜单，让「立即刷新电量」的可用状态跟着变。"""
        icon = self.icon
        if icon is None:
            return
        try:
            icon.update_menu()
        except Exception:
            log.debug("重建托盘菜单失败", exc_info=True)

    # --- 启动提示 ---
    def _device_summary_lines(self, only_low: bool = False, devices=None):
        """按电量从低到高排，有线/未知电量排最后；超过上限折叠成一行。"""
        rs = self.snapshot()
        threshold = int(self.cfg.get("low_threshold", 20))
        items = []
        for d in (self._devices if devices is None else devices):
            r = rs.get(d.key)
            if not r or not r.connected:
                continue
            if only_low and (r.wired or r.level is None or r.level > threshold):
                continue
            items.append((d, r))

        def sort_key(item):
            d, r = item
            if not r.wired and r.level is not None:
                return (0, r.level, "")
            return (1, 0, d.name())

        items.sort(key=sort_key)
        lines = []
        for d, r in items[:SUMMARY_MAX_LINES]:
            if r.wired:
                lines.append("%s · USB 供电" % d.name())
            elif r.level is not None:
                lines.append("%s · %d%%" % (d.name(), r.level))
            else:
                lines.append("%s · 已连接" % d.name())
        if len(items) > SUMMARY_MAX_LINES:
            # 这里 items 是全部设备，不是"剩下的"，所以用"共"而不是"等"
            lines.append("共 %d 台设备" % len(items))
        return lines, len(items)

    def _startup_notify(self):
        """手动启动时汇报一遍设备；有低电量就直接报警；自动启动时只在低电量时出声。"""
        if self.stop_evt.is_set():
            return
        if not self.notify_ok:
            # 系统不让发通知，硬发也只会被吞掉
            log.info("通知权限不可用，跳过启动提示")
            return
        try:
            # 开机自启时设备栈可能还没就绪，第一次扫描会拿到空表；
            # 这里补几次重试，免得通知说「当前没有检测到设备」，几秒后设备才冒出来
            for _ in range(3):
                if self._devices:
                    break
                time.sleep(1.0)
                self.rebuild_devices()
            low_lines, low_count = self._device_summary_lines(only_low=True)
            if low_count and not self.mute_alerts:
                show_toast("电量偏低，请及时充电", "\n".join(low_lines), self.alert_png)
                now = time.time()
                rs = self.snapshot()
                threshold = int(self.cfg.get("low_threshold", 20))
                for d in list(self._devices):
                    r = rs.get(d.key)
                    if (r and r.connected and not r.wired
                            and r.level is not None and r.level <= threshold):
                        self.alert_sent[d.key] = now
                log.info("启动时发现 %d 台低电量设备，已提示", low_count)
                return

            if self.autostart:
                log.info("自动启动且无低电量设备，不发送提示")
                return

            lines, _ = self._device_summary_lines()
            if not lines:
                lines = ["当前没有检测到设备"]
            show_toast("外设电量监视已启动", "\n".join(lines), self.connect_png, silent=True)
            log.info("启动提示: %s", " / ".join(lines))
        except Exception:
            log.exception("启动提示失败")

    # --- 提醒 ---
    def _maybe_alert(self, dev: BaseDevice, rd: Reading):
        # 启动初期的低电量统一由 _startup_notify 汇总，避免两条通知打架
        if time.time() - self.boot_time < 6:
            return
        if not rd.connected or rd.level is None or rd.wired:
            self.alert_sent.pop(dev.key, None)
            return
        threshold = int(self.cfg.get("low_threshold", 20))
        if rd.level > threshold:
            self.alert_sent.pop(dev.key, None)
            return
        now = time.time()
        if now - self.alert_sent.get(dev.key, 0) < self.cfg.get("alert_repeat_min", 30) * 60:
            return
        if self.mute_alerts:
            return          # 临时停用：不发通知、也不记冷却，图标照旧闪烁
        self.alert_sent[dev.key] = now
        show_toast("%s 电量偏低" % dev.name(),
                   "当前剩余 %d%%，请及时充电。" % rd.level,
                   self.alert_png)
        log.info("已发送低电量通知: %s %d%%", dev.name(), rd.level)

    def check_threshold_now(self):
        """阈值改了：立刻按新阈值评估一遍，已经低于阈值的马上提醒。"""
        self.alert_sent.clear()
        rs = self.snapshot()
        for d in list(self._devices):
            rd = rs.get(d.key)
            if rd is not None:
                self._maybe_alert(d, rd)
        self.apply(force=True)

    def _on_connected(self, dev: BaseDevice, rd: Reading):
        if not self.cfg.get("notify_on_connect", True):
            return
        if time.time() - self.boot_time < 8:
            log.info("[%s] 启动阶段已在线，跳过接入提示", dev.name())
            return
        if time.time() - self.connect_sent.get(dev.key, 0) < 600:
            return
        self.connect_sent[dev.key] = time.time()
        if rd.wired:
            msg = "使用有线连接，正在供电"
        elif rd.level is not None:
            how = "蓝牙连接" if getattr(dev.info, "kind", "") == "bt" else "无线连接"
            msg = "%s · 剩余电量 %d%%" % (how, rd.level)
        else:
            msg = "已连接"
        show_toast("%s 已连接" % dev.name(), msg, self.connect_png, silent=True)
        log.info("设备接入提示: %s / %s", dev.name(), msg)

    # --- 托盘 ---
    def snapshot(self):
        with self.lock:
            return dict(self.readings)

    def compute(self):
        rs = self.snapshot()
        items = []
        for d in list(self._devices):
            r = rs.get(d.key)
            if r:
                items.append((d, r))

        connected = [(d, r) for d, r in items if r.connected]
        wireless = [(d, r) for d, r in connected if not r.wired and r.level is not None]
        threshold = int(self.cfg.get("low_threshold", 20))
        sz = tray_icon_size()       # 按 DPI 出图，避免系统重采样导致发虚
        alarm = any(r.level is not None and r.level <= threshold for _, r in wireless)
        badged = not self.notify_ok     # 通知权限不可用 → 右下角黄色角标
        have_profiles = bool(PROFILES) or bool(_BT_CACHE["items"])  # 配置或蓝牙设备都算有来源
        logo = app_icon_image(sz, badge=badged)

        if alarm:
            # 低电量才亮警示图标（并在 ui_loop 里闪烁）
            img = icon_alarm(sz)
        elif not have_profiles:
            # 没有任何配置文件：再好的设备也读不到，优先级与通知权限相同
            if badged:
                # 两样都不行：压暗 + 黄色角标
                img = app_icon_image(sz, 0.45, True) or icon_warn(sz)
            else:
                img = icon_warn(sz)
        elif connected:
            # 电量正常：直接用软件图标，避免和系统电池图标混淆
            img = logo if logo is not None else icon_wired(sz)
        else:
            # 一台都没连上：同一个软件图标压暗，既保持辨识度又提示状态异常
            img = app_icon_image(sz, 0.45, badged) or icon_offline(sz)

        lines = []
        for d in list(self._devices):
            r = rs.get(d.key)
            nm = d.name()
            if r is None:
                # 刚被纳入监视、还没读完第一轮：不能直接跳过，
                # 否则换连接方式后 tooltip 会整行消失，看起来像没更新
                lines.append("%s：准备中" % nm)
                continue
            if not r.connected:
                why = "未连接（设备已关机）" if "关机" in (r.error or "") else "未连接"
                lines.append("%s：%s" % (nm, why))
            elif r.wired:
                lines.append("%s：使用有线连接" % nm)
            elif r.level is not None:
                lines.append("%s，剩余电量%d%%" % (nm, r.level))
            else:
                lines.append("%s：已连接" % nm)
        if not lines:
            lines.append("未监视任何设备" if self._devices else "正在读取…")
        if not have_profiles:
            lines = ["未加载设备配置"]
        tip = "\n".join(lines)
        if not have_profiles:
            tip = _with_hint(lines, [
                "把设备配置文件放进配置文件夹后即可监视",
                "单击此处打开配置文件列表",
            ])
        elif badged:
            tip = _with_notify_warning(lines)
        if time.time() < self.refresh_until:
            tip = "正在刷新…\n" + tip
        return img, tip, alarm

    def apply(self, force=False):
        if self.icon is None:
            return False
        img, tip, alarm = self.compute()
        # 必须用完整像素做签名：不同图标左上角都是透明，只比前几十字节会漏更新
        sig = (tip, alarm, img.tobytes())
        now = time.time()
        changed = sig != self._last_sig
        # 内容没变也每 5 秒重推一次，保证系统缓存的 szTip 不会过期
        if not (force or changed or now - self._last_title_sync > 5):
            return alarm
        self._last_sig = sig
        self._last_title_sync = now
        try:
            if force or changed:
                self.icon.icon = img
            self.icon.title = tip[:125]
            # Windows 在 tooltip 已经弹出时不会自动重绘，
            # 内容变了就再推一次，尽量让它当场刷新
            if changed:
                upd = getattr(self.icon, "_update_title", None)
                if callable(upd):
                    upd()
        except Exception:
            pass
        return alarm

    def ui_loop(self):
        blink = False
        was_flashing = False
        while not self.stop_evt.is_set():
            try:
                # 定期重新枚举，新插上的设备能自动纳入监视，
                # 换连接方式（PID 变化）也不会留下幽灵条目
                now = time.time()
                if self._need_rescan or now - self._last_scan > 5:
                    self._need_rescan = False
                    self._last_scan = now
                    self.rebuild_devices()
                alarm = self.apply()
                if self._need_menu_refresh:
                    self._need_menu_refresh = False
                    self._refresh_menu()
                flashing = bool(alarm or time.time() < self._flash_until)
                if flashing:
                    # 低电量（或自检后）警示图标 ↔ 软件图标交替，形成闪烁
                    blink = not blink
                    sz = tray_icon_size()
                    logo = app_icon_image(sz, badge=not self.notify_ok)
                    self.icon.icon = (icon_alarm(sz) if blink
                                      else (logo if logo is not None else icon_wired(sz)))
                elif was_flashing:
                    # 闪烁刚结束：强制推一次常态图标。
                    # 否则若最后一下停在警告图标上，签名没变化就再也不会回退
                    blink = False
                    self.apply(force=True)
                was_flashing = flashing
                if now - self._last_notify_check > NOTIFY_CHECK_SEC:
                    self._last_notify_check = now
                    # ① 配置文件：和通知权限同等优先级，同一拍一起查
                    had = bool(PROFILES)
                    if not had:
                        reload_profiles()      # 刚补进目录的文件也能自动接上
                    if bool(PROFILES) != had:
                        log.info("设备配置状态: %s", "已就绪" if PROFILES else "缺失")
                        try:
                            new = self.reload_devices()
                            if new:
                                threading.Thread(target=self.notify_added,
                                                 args=(new,), daemon=True).start()
                        except Exception:
                            log.exception("按新配置重建设备失败")
                    # ② 通知权限：读系统 API，不弹任何东西
                    ok, notes = notifications_available()
                    if ok != self.notify_ok or notes != self.notify_notes:
                        self.notify_ok, self.notify_notes = ok, notes
                        if ok:
                            log.info("通知权限: 可用")
                        else:
                            log.warning("通知权限: 不可用（%s）", "；".join(notes))
                    self.apply(force=True)
            except Exception:
                # 别把异常吞掉：限频记录，避免某个异常每 0.8 秒刷满日志
                now = time.time()
                if now - self._loop_err_ts > 60:
                    self._loop_err_ts = now
                    log.exception("主循环异常（60 秒内同类只记一次）")
            time.sleep(0.8)

    # --- 菜单动作 ---
    def on_activate(self):
        """左键单击托盘图标：先解决配置文件，再看通知权限，都正常才进设置。"""
        if not PROFILES and not _BT_CACHE["items"]:
            self.open_profiles_window()
        elif not self.notify_ok:
            self.open_notify_settings()
        else:
            self.open_settings()

    def open_profiles_window(self):
        if self.root is None:
            return
        self.root.after(0, self._show_profiles)

    def _show_profiles(self):
        d = self.profiles_dialog
        if d is not None:
            try:
                d.win.deiconify()
                d.win.lift()
                d.win.focus_force()
                return
            except Exception:
                self.profiles_dialog = None
        try:
            self.profiles_dialog = ProfilesDialog(None, self)
            log.info("配置文件窗口已打开（托盘）")
        except Exception:
            log.exception("打开配置文件窗口失败")

    def _close_profiles(self):
        d = self.profiles_dialog
        self.profiles_dialog = None
        if d is not None:
            try:
                d.close(from_settings=True)
            except Exception:
                pass

    def open_notify_settings(self):
        try:
            os.startfile("ms-settings:notifications")
            log.info("已打开系统通知设置")
        except Exception as e:
            log.warning("打开通知设置失败: %s", e)

    def open_settings(self, icon=None, item=None):
        if self.root is None:
            return
        self.root.after(0, self._show_settings)

    def _show_settings(self):
        # 从托盘进设置时，先把之前从托盘单独打开的配置文件窗口关掉
        self._close_profiles()
        if self.settings_dialog is not None:
            try:
                self.settings_dialog.win.deiconify()
                self.settings_dialog.win.lift()
                return
            except Exception:
                self.settings_dialog = None
        try:
            self.settings_dialog = SettingsDialog(self)
            log.info("设置窗口已打开")
        except Exception as e:
            log.exception("打开设置窗口失败")
            ctypes.windll.user32.MessageBoxW(None, "无法打开设置：%s" % e,
                                             APP_NAME, 0x10)

    def on_config_changed(self):
        self.alert_sent.clear()
        self.rebuild_devices()
        self.apply(force=True)
        self._need_menu_refresh = True      # 阈值/自定义项可能变了
        self.check_threshold_now()          # 阈值变了要立刻重新评估

    def toggle_mute(self):
        """临时停用低电量通知：不弹通知，但托盘照旧闪。只在本次运行有效。"""
        self.mute_alerts = not self.mute_alerts
        self._need_menu_refresh = True
        log.info("低电量通知: %s", "已临时停用" if self.mute_alerts else "已恢复")
        if not self.mute_alerts:
            self.check_threshold_now()   # 恢复时若有低电量设备，立刻补一条
        self.apply(force=True)

    def set_threshold(self, value):
        # 菜单回调是 action(icon, item)，早年这里签名写错过，
        # 把 Icon 对象当成了阈值；这里再兜一层，脏值一律丢弃
        try:
            value = max(1, min(99, int(value)))
        except Exception:
            log.warning("忽略无效的阈值: %r", value)
            return
        self.cfg["low_threshold"] = value
        if not save_config(self.cfg):
            self._warn_save_failed()
        self.check_threshold_now()      # 新阈值下已达标的设备立刻提醒
        self._need_menu_refresh = True  # 打勾项要跟着动
        self._sync_threshold_to_ui(value)

    def _warn_save_failed(self):
        """保存失败只提示一次，避免连续操作时反复弹窗。"""
        if getattr(self, "_save_warned", False):
            return
        self._save_warned = True
        log.error("配置保存失败（程序目录可能不可写）：%s", CFG_PATH)
        try:
            self.root.after(0, lambda: show_error_dialog(
                self.root,
                "设置已在本次运行生效，但写入 config.json 失败。\n"
                "程序目录可能没有写权限，重启后这些设置会丢失。"))
        except Exception:
            pass

    def _sync_threshold_to_ui(self, value):
        """菜单改阈值后，把界面上的阈值控件同步过去（设置窗口开着才有意义）。"""
        dlg = self.settings_dialog
        if dlg is None or self.root is None:
            return
        try:
            self.root.after(0, lambda: dlg.on_threshold_changed(value))
        except Exception:
            pass

    def threshold_menu(self):
        # 注意：pystray 的 action 只认 0/1/2 个参数，
        # 1 个参数时收到的是 icon，所以这里用工厂函数固定阈值，写足两个形参
        def make(v):
            return lambda icon, item: self.set_threshold(v)

        # 用「可调用菜单」而不是固定列表：pystray 的 update_menu() 只重建
        # HMENU，静态列表不会重新求值，运行中新加的自定义阈值就不会出现。
        # 传生成器给 Menu，每次重建菜单时才会重新算一遍条目。
        def gen():
            for v in PRESET_THRESHOLDS:
                yield MenuItem(
                    "%d%%" % v, make(v),
                    checked=(lambda i, v=v: int(self.cfg.get("low_threshold", 20)) == v),
                    radio=True)
            custom = int(self.cfg.get("custom_threshold", 0) or 0)
            if 0 < custom <= 99 and custom not in PRESET_THRESHOLDS:
                # 自定义阈值只有在设置界面保存过之后才出现
                yield MenuItem(
                    "%d%%（自定义）" % custom, make(custom),
                    checked=(lambda i, v=custom:
                             int(self.cfg.get("low_threshold", 20)) == v),
                    radio=True)

        return Menu(gen)

    def toggle_autostart(self, icon=None, item=None):
        self.cfg["start_with_windows"] = not self.cfg.get("start_with_windows", False)
        if not save_config(self.cfg):
            self._warn_save_failed()
        apply_autostart(self.cfg["start_with_windows"])

    def self_check(self, icon=None, item=None):
        def work():
            # 先立刻复查通知权限：刚在系统设置里改过的话，
            # 点一下自检就能马上反映到角标和悬停提示上
            ok, notes = notifications_available()
            self.notify_ok, self.notify_notes = ok, notes
            self._last_notify_check = time.time()
            self.apply(force=True)

            lines = []
            for d in list(self._devices):
                try:
                    rd = d.poll_forced()
                except Exception as e:
                    lines.append("· %s：读取异常（%s）" % (d.name(), e))
                    continue
                if rd.connected and rd.level is not None:
                    lines.append("· %s：已连接，电量 %d%%" % (d.name(), rd.level))
                elif rd.connected:
                    lines.append("· %s：已连接，%s" % (d.name(), "USB 供电" if rd.wired else "在线"))
                else:
                    lines.append("· %s：无法读取（%s）" % (d.name(), rd.error or "未连接"))
            if not lines:
                lines.append("· 当前没有勾选任何设备")

            show_toast("测试通知", "看到这条通知，说明低电量提醒可以正常工作。", self.alert_png)
            # 自检时让托盘图标闪一下，肉眼确认图标通路也正常
            self._flash_until = time.time() + 2.4
            self.apply(force=True)
            body = "设备状态：\n" + "\n".join(lines) + "\n\n通知："
            if ok and not notes:
                body += "已发送一条测试通知。\n若没有看到，请检查「设置 → 系统 → 通知」。"
            else:
                body += "存在限制：\n  " + "\n  ".join(notes) + "\n\n请在系统设置里放开后重试。"
            log.info("自检:\n%s", body)
            try:
                mb = ctypes.windll.user32.MessageBoxW
                mb.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                               ctypes.c_wchar_p, ctypes.c_uint]
                mb.restype = ctypes.c_int
                mb(None, body, "外设电量监视 · 自检", 0x40)
            except Exception as e:
                log.warning("自检弹窗失败: %s", e)

        threading.Thread(target=work, daemon=True).start()

    def open_log(self, icon=None, item=None):
        if not os.path.exists(LOG_PATH):
            show_error_dialog(self.root,
                              "当前没有日志文件：\n%s\n\n"
                              "可能是程序目录不可写，或还没有产生日志。" % LOG_PATH)
            return
        try:
            subprocess.Popen(["notepad.exe", LOG_PATH])
        except Exception as e:
            log.warning("打开日志失败: %s", e)

    def quit(self, icon=None, item=None):
        self.stop_evt.set()
        for d in list(self._devices):
            if d._evt:
                d._evt.set()       # 工作线程醒来后会自己 close
        if self.root is not None:
            try:
                self.root.after(0, self.root.destroy)
            except Exception:
                pass
        if self.icon:
            self.icon.stop()

    # --- 启动 ---
    def run(self):
        self.root = tk.Tk()
        self.root.withdraw()
        try:
            self.root.tk.call("tk", "scaling", system_dpi() / 72.0)
        except Exception:
            pass
        try:
            self._root_icon = tk.PhotoImage(file=self.icon_png)
            self.root.iconphoto(True, self._root_icon)
        except Exception as e:
            log.debug("根窗口图标设置失败: %s", e)

        if not _LOG_FILE_OK:
            self._check_log_writable()
        if (not CONFIG_EXISTED) and (not CONFIG_WAS_RESET):
            self._show_welcome()

        self.rebuild_devices()
        threading.Thread(target=self.ui_loop, daemon=True).start()
        threading.Thread(target=self._tray_thread, daemon=True).start()

        if "--settings" in sys.argv:
            self.root.after(300, self._show_settings)
        # 等第一轮读数回来再汇报
        self.root.after(3500, self._startup_notify)

        log.info("==== 启动 v%s ==== (autostart=%s)", APP_VERSION, self.autostart)
        self.root.mainloop()

    def _check_log_writable(self):
        """程序目录不可写时的提示：继续（不写日志）或退出。"""
        log.warning("日志文件无法写入：%s（%s）", LOG_PATH, _LOG_FILE_ERR)
        if ask_yes_no(
                "无法写入日志",
                "无法在当前目录写入日志文件，请把程序移动到可读写目录运行。\n\n"
                "【是】：继续运行（本次不写日志文件）\n"
                "【否】：退出程序"):
            log.info("用户选择继续运行（本次不写日志文件）")
            return
        log.info("用户选择退出（日志不可写）")
        sys.exit(0)

    def _show_welcome(self):
        try:
            self.welcome_dialog = WelcomeDialog(self)
        except Exception:
            log.exception("欢迎引导窗口创建失败，跳过引导")
            self.welcome_dialog = None

    def _tray_thread(self):
        menu = Menu(
            MenuItem("设置…", lambda i, it: self.on_activate(), default=True),
            MenuItem("立即刷新电量", lambda i, it: self.refresh_now(),
                     enabled=lambda it: bool(self._devices)),
            MenuItem("临时停用低电量通知", lambda i, it: self.toggle_mute(),
                     checked=lambda it: bool(self.mute_alerts)),
            Menu.SEPARATOR,
            MenuItem("低电量提醒阈值", self.threshold_menu()),
            MenuItem("运行自检（设备 + 通知）", lambda i, it: self.self_check()),
            MenuItem("查看日志", lambda i, it: self.open_log()),
            Menu.SEPARATOR,
            MenuItem("退出", lambda i, it: self.quit()),
        )
        _sz0 = tray_icon_size()
        self.icon = pystray.Icon(
            APP_ID, app_icon_image(_sz0) or icon_offline(_sz0), APP_NAME, menu)
        try:
            self.icon.run()
        except Exception:
            log.exception("托盘启动失败")


# ---------------------------------------------------------------- 自启动

def apply_autostart(enable: bool):
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        if enable:
            exe = sys.executable
            if getattr(sys, "frozen", False):
                cmd = '"%s" %s' % (exe, AUTOSTART_FLAG)
            else:
                cmd = '"%s" "%s" %s' % (exe, os.path.abspath(sys.argv[0]), AUTOSTART_FLAG)
            winreg.SetValueEx(key, APP_ID, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_ID)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        log.info("开机自启: %s", enable)
    except Exception as e:
        log.warning("设置开机自启失败: %s", e)


# ---------------------------------------------------------------- 单实例

_mutex_handle = None


def single_instance() -> bool:
    """同一时间只允许一个实例：同一个程序放到哪个目录都算同一个。

    用命名互斥体判断，进程活着就一直持有；第二个实例会拿到
    ERROR_ALREADY_EXISTS，直接返回 False。"""
    global _mutex_handle
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 不声明 restype 时 ctypes 按 c_int 返回，64 位下句柄会被截断
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                     ctypes.c_wchar_p]
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = ctypes.c_int
        handle = k32.CreateMutexW(None, False, "Local\\%s" % APP_ID)
        err = ctypes.get_last_error()
        if not handle:
            return True                 # 连句柄都建不出来，就别拦着用户了
        if err == 183:                  # ERROR_ALREADY_EXISTS
            try:
                k32.CloseHandle(handle)
            except Exception:
                pass
            return False
        _mutex_handle = handle          # 进程存活期间一直持有
        return True
    except Exception:
        return True


def show_startup_error() -> None:
    """第二个实例启动失败时的提示（置于最前，免得被别的窗口挡住）。"""
    try:
        mb = ctypes.windll.user32.MessageBoxW
        mb.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                       ctypes.c_wchar_p, ctypes.c_uint]
        mb.restype = ctypes.c_int
        # MB_SETFOREGROUND | MB_TOPMOST | MB_ICONWARNING
        mb(None, "已经有一个实例正在运行，请检查系统托盘或任务管理器",
           "启动失败", 0x00010000 | 0x00040000 | 0x30)
    except Exception as e:
        log.warning("单实例提示弹窗失败: %s", e)


def main():
    global CFG
    enable_dpi_awareness()          # 只为让提示框清晰，不改任何状态
    # 绝对优先：已有实例就直接提示退出，不写配置、不建托盘、不加载设备
    if not single_instance():
        log.info("已有实例在运行，已提示并退出")
        show_startup_error()
        return
    # 到这里才读配置：损坏时的弹窗/重置只会发生在真正的首个实例里
    CFG = load_config()
    if not os.path.exists(CFG_PATH):
        save_config(CFG)
    if CFG.get("start_with_windows"):
        apply_autostart(True)      # 重写一次，确保命令里带上 --autostart
    App().run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("主程序异常")
        raise
