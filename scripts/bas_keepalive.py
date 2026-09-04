#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SAP BAS Keep Alive + 健康检查
================================================================
融合两份经过实战验证的流程：
  1. 登录保活（参考 zaofengyue/sap-bas-keepalive 的 JS 版逻辑）
     登录 BTP -> 处理隐私弹窗 -> 检查 dev space 状态
     -> 若 STOPPED 则启动并等待 RUNNING -> 进入编辑器停留
  2. 应用健康检查（来自已验证的 bas_devspace_restart_test.py）
     进入 IDE 后在终端执行 supervisord 状态查询，
     逐一断言服务 RUNNING，可选自动修复（supervisorctl start all）

环境变量（均可通过 GitHub Secrets / workflow env 配置）：
  BAS_URL            必填，BAS 地址，多账号用 ';' 分隔
  BTP_USER           必填，登录邮箱，多账号用 ';' 分隔
  BTP_PASSWORD       必填，登录密码，多账号用 ';' 分隔
  BAS_SPACE_NAME     选填，dev space 名称，多账号用 ';' 分隔
  HEALTHCHECK_ENABLED 默认 true，是否执行终端健康检查
  HEALTHCHECK_TASKS  默认 6 个服务名，';' 分隔；设为空跳过逐一断言
  SUPERVISOR_CONF    默认 ~/.config/supervisor/supervisord.conf
  AUTO_FIX           默认 true，发现任务未 RUNNING 时自动 start all 并复查
  STAY_SECONDS       默认 60，编辑器内停留时长（记录活跃）
  BOOTSTRAP_WAIT_SEC 默认 40，等待 .bashrc 钩子引导 supervisord
  START_TIMEOUT_SEC  默认 300，等待 dev space 变 RUNNING 的超时
  EDITOR_TIMEOUT_SEC 默认 180，等待编辑器加载的超时
  HEADLESS           默认 true；本地调试可设 false
  FAIL_ON_UNHEALTHY  默认 true，健康检查失败时进程退出码非 0（Actions 标红）
"""

import html
import json
import os
import re
import sys
import time
import urllib.request
import uuid

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------------------- 配置 ----------------------------

def env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


HEADLESS = env_bool("HEADLESS", True)
HEALTHCHECK_ENABLED = env_bool("HEALTHCHECK_ENABLED", True)
AUTO_FIX = env_bool("AUTO_FIX", True)
FAIL_ON_UNHEALTHY = env_bool("FAIL_ON_UNHEALTHY", True)

STAY_SECONDS = env_int("STAY_SECONDS", 60)
BOOTSTRAP_WAIT_SEC = env_int("BOOTSTRAP_WAIT_SEC", 40)
START_TIMEOUT_SEC = env_int("START_TIMEOUT_SEC", 300)
EDITOR_TIMEOUT_SEC = env_int("EDITOR_TIMEOUT_SEC", 180)

SUPERVISOR_CONF = os.environ.get(
    "SUPERVISOR_CONF", "~/.config/supervisor/supervisord.conf"
)
DEFAULT_TASKS = "cloudflared;komari-agent;opencode-telegram-bot;ttyd;vscode-terminal-task;xray"
HEALTHCHECK_TASKS = [
    t.strip() for t in os.environ.get("HEALTHCHECK_TASKS", DEFAULT_TASKS).split(";")
    if t.strip()
]

LOGIN_TIMEOUT_MS = 60_000
HEALTH_CMD_TMPL = (
    'echo HEALTHCHECK-$(date +%H:%M:%S); '
    'P=$(pgrep -x supervisord | head -1); echo SV-PID=$P; '
    'test -f /tmp/.sv-bootstrapped && echo MARKER-EXISTS || echo NO-MARKER; '
    'supervisorctl -c {conf} status 2>&1'
)


def parse_accounts():
    urls = [s.strip() for s in os.environ.get("BAS_URL", "").split(";") if s.strip()]
    users = [s.strip() for s in os.environ.get("BTP_USER", "").split(";") if s.strip()]
    pwds = [s.strip() for s in os.environ.get("BTP_PASSWORD", "").split(";") if s.strip()]
    spaces = [s.strip() for s in os.environ.get("BAS_SPACE_NAME", "").split(";") if s.strip()]
    if not urls:
        log("MAIN", "❌ BAS_URL 未配置")
        sys.exit(1)
    return [
        {
            "bas_url": urls[i],
            "user": users[i] if i < len(users) else (users[0] if users else ""),
            "password": pwds[i] if i < len(pwds) else (pwds[0] if pwds else ""),
            "space": spaces[i] if i < len(spaces) else (spaces[0] if spaces else ""),
        }
        for i in range(len(urls))
    ]


# ---------------------------- 日志 ----------------------------

def log(tag: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [{tag}] {msg}", flush=True)


def gh_summary(text: str) -> None:
    """写入 GitHub Actions Job Summary（本地运行时忽略）。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass


# ------------------------ Telegram 通知 ------------------------

def send_telegram_notification(
    title: str,
    details: list,
    photo_bytes: bytes = None,
    is_success: bool = True,
) -> bool:
    """发送 Telegram 消息或截图通知（若未配置环境变量则静默跳过）。"""
    token = os.environ.get("TG_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    api_host = os.environ.get("TG_API_HOST") or os.environ.get("TELEGRAM_API_HOST") or "api.telegram.org"
    api_host = api_host.strip().rstrip("/")
    if not api_host.startswith("http://") and not api_host.startswith("https://"):
        api_host = f"https://{api_host}"

    icon = "✅" if is_success else "❌"
    lines = [f"{icon} <b>{html.escape(title)}</b>", ""]
    for label, val in details:
        lines.append(f"<b>{html.escape(str(label))}</b>: <code>{html.escape(str(val))}</code>")
    caption = "\n".join(lines)
    if len(caption) > 1024:
        caption = caption[:1020] + "..."

    try:
        if photo_bytes:
            url = f"{api_host}/bot{token}/sendPhoto"
            boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
            body = bytearray()

            def add_field(name: str, value: str):
                body.extend(f"--{boundary}\r\n".encode("utf-8"))
                body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
                body.extend(f"{value}\r\n".encode("utf-8"))

            add_field("chat_id", str(chat_id))
            add_field("caption", caption)
            add_field("parse_mode", "HTML")

            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(b'Content-Disposition: form-data; name="photo"; filename="screenshot.png"\r\n')
            body.extend(b"Content-Type: image/png\r\n\r\n")
            body.extend(photo_bytes)
            body.extend(b"\r\n")
            body.extend(f"--{boundary}--\r\n".encode("utf-8"))

            req = urllib.request.Request(
                url,
                data=bytes(body),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
        else:
            url = f"{api_host}/bot{token}/sendMessage"
            payload = json.dumps({
                "chat_id": str(chat_id),
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

        with urllib.request.urlopen(req, timeout=30) as resp:
            log("TG", f"Telegram 通知已发送 ({resp.status})")
            return True
    except Exception as exc:
        log("TG", f"发送 Telegram 通知失败: {exc}")
        return False


# ------------------------ 页面查找辅助 ------------------------

def iter_frames(page):
    """主页面 + 所有 iframe（BAS 大量内容嵌在 iframe 中）。"""
    try:
        return list(page.frames)
    except Exception:
        return [page.main_frame]


def find_in_frames(page, selector: str, timeout_ms: int = 3000):
    """在所有 frame 中查找第一个匹配的 locator，超时返回 None。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for frame in iter_frames(page):
            try:
                loc = frame.locator(selector).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue
        time.sleep(0.5)
    return None


def click_if_found(page, selectors, timeout_ms: int = 3000) -> bool:
    """依次尝试 selectors，找到可见的就点击。"""
    loc = find_in_frames(page, ",".join(selectors), timeout_ms) \
        if all(isinstance(s, str) for s in selectors) else None
    if loc:
        try:
            loc.click(timeout=3000)
            return True
        except Exception:
            pass
    for sel in selectors:  # 逐个重试（组合选择器可能因单帧不可见而漏检）
        l2 = find_in_frames(page, sel, 1500)
        if l2:
            try:
                l2.click(timeout=3000)
                return True
            except Exception:
                continue
    return False


# ------------------------ 通用弹窗处理 ------------------------

def dismiss_dialog(page, timeout_ms: int = 8000) -> bool:
    """处理 BAS 首次登录的隐私声明等弹窗：勾选复选框后点 OK/Accept。"""
    btn = find_in_frames(
        page,
        'button:has-text("OK"), button:has-text("Accept"), button:has-text("Agree")',
        timeout_ms,
    )
    if not btn:
        return False
    # 在所有 frame 内勾选可见复选框
    for frame in iter_frames(page):
        try:
            cbs = frame.locator('input[type="checkbox"]')
            for i in range(min(cbs.count(), 3)):
                c = cbs.nth(i)
                if c.is_visible() and not c.is_checked():
                    c.click()
                    time.sleep(0.5)
        except Exception:
            continue
    try:
        btn.click()
        time.sleep(1.5)
        return True
    except Exception:
        return False


def dismiss_ide_notifications(page) -> None:
    """处理 IDE 内通知（来自实战记录）：
    - 恶意文件警告 -> 点 Ignore（绝不删除用户文件）
    - 普通通知     -> Clear Notification
    """
    for frame in iter_frames(page):
        try:
            for btn in frame.get_by_role(
                "button", name=re.compile(r"^\s*Ignore\s*$", re.I)
            ).all():
                if btn.is_visible():
                    btn.click()
                    log("NOTIF", "已忽略恶意文件警告（保留文件）")
                    time.sleep(0.8)
        except Exception:
            continue
        try:
            for btn in frame.locator("button[title*='Clear Notification']").all():
                if btn.is_visible():
                    btn.click()
                    time.sleep(0.5)
        except Exception:
            continue


# --------------------------- 登录 ---------------------------

USER_FIELD = 'input[type="email"], input[name="logonuidfield"], #j_username'
PWD_FIELD = 'input[type="password"], #j_password'
SUBMIT_FIELD = ('button[type="submit"], #logOnFormSubmit, '
                'button:has-text("Sign In"), button:has-text("Log On")')


def login(page, bas_url: str, user: str, password: str) -> None:
    log("LOGIN", f"打开 {bas_url}")
    page.goto(bas_url, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)

    # 已有会话则跳过登录表单
    if find_in_frames(page, USER_FIELD, 8000) is None:
        try:
            page.wait_for_url(re.compile(r"applicationstudio\.cloud\.sap"),
                              timeout=15000)
            log("LOGIN", "✅ 已存在会话，免登录")
            return
        except PWTimeout:
            pass

    log("LOGIN", "📧 输入邮箱...")
    field = find_in_frames(page, USER_FIELD, 30000)
    if not field:
        raise RuntimeError("未找到登录用户名输入框（页面结构可能已变化）")
    field.fill(user)
    time.sleep(1)
    # 新版 BTP 登录：邮箱 -> Continue -> 密码；经典表单：同页双输入框
    click_if_found(
        page,
        ['button:has-text("Continue")', 'button:has-text("Next")', "#continue"],
        3000,
    )

    log("LOGIN", "🔑 输入密码...")
    pwd = find_in_frames(page, PWD_FIELD, 20000)
    if not pwd:
        raise RuntimeError("未找到密码输入框")
    pwd.fill(password)
    time.sleep(0.5)

    if not click_if_found(page, SUBMIT_FIELD.split(", "), 5000):
        page.keyboard.press("Enter")
    log("LOGIN", "等待跳转 BAS ...")
    page.wait_for_url(re.compile(r"applicationstudio\.cloud\.sap"),
                      timeout=LOGIN_TIMEOUT_MS)
    log("LOGIN", "✅ 登录成功")


# ---------------------- dev space 状态管理 ----------------------

STATUS_RE = re.compile(r"^(RUNNING|STOPPED|STARTING|STOPPING)$", re.I)


def read_space_status(page) -> str:
    """读取 dev space 状态：优先状态文本，回退到参考项目的 CSS 类选择器。"""
    for frame in iter_frames(page):
        try:
            els = frame.get_by_text(STATUS_RE)
            for el in els.all():
                t = (el.inner_text() or "").strip().upper()
                if t in ("RUNNING", "STOPPED", "STARTING", "STOPPING"):
                    return t
        except Exception:
            continue
    # 回退：参考项目 bas-login.js 的选择器
    for frame in iter_frames(page):
        try:
            if frame.locator("a.stoppedStatus").count() > 0:
                return "STOPPED"
            if frame.locator("a.hyperlink:not(.disabled)").count() > 0:
                return "RUNNING"
        except Exception:
            continue
    return "UNKNOWN"


def start_space_if_stopped(page, tag: str) -> None:
    status = read_space_status(page)
    log(tag, f"📊 dev space 状态: {status}")

    if status in ("RUNNING", "STARTING"):
        return

    log(tag, "▶️ dev space 未运行，点击启动 ...")
    started = click_if_found(
        page,
        [
            "#startButton0",
            'button[title*="Start"]',
            'button[aria-label*="Start"]',
        ],
        8000,
    )
    if not started:
        raise RuntimeError("未找到启动按钮（页面结构可能已变化）")
    log(tag, "✅ 已点击启动，等待 RUNNING ...")

    deadline = time.time() + START_TIMEOUT_SEC
    while time.time() < deadline:
        time.sleep(10)
        try:
            page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        dismiss_dialog(page, 3000)
        status = read_space_status(page)
        log(tag, f"   等待中... 剩余 {int(deadline - time.time())}s，当前 {status}")
        if status == "RUNNING":
            log(tag, "✅ dev space 已 RUNNING")
            return
    raise TimeoutError(f"{START_TIMEOUT_SEC}s 内 dev space 未达到 RUNNING")


def enter_workspace(page, context, space: str, tag: str):
    """点击空间链接进入编辑器；编辑器可能在当前页（hash 路由）或新标签打开。"""
    log(tag, "🖱️ 进入 dev space 编辑器 ...")
    new_pages = []
    context.on("page", lambda p: new_pages.append(p))

    link = None
    for frame in iter_frames(page):
        try:
            cand = frame.locator('a.hyperlink:not(.disabled)[href*="#ws-"]').first
            if cand.count() > 0:
                link = cand
                break
            if space:
                cand = frame.locator(
                    f'a.hyperlink:not(.disabled):has-text("{space}")'
                ).first
                if cand.count() > 0:
                    link = cand
                    break
        except Exception:
            continue
    if link:
        link.click()
    else:
        raise RuntimeError("未找到可点击的空间链接")

    time.sleep(3)

    def is_editor(url: str) -> bool:
        return "#ws-" in url

    deadline = time.time() + EDITOR_TIMEOUT_SEC
    while time.time() < deadline:
        if is_editor(page.url):
            log(tag, "✅ 编辑器已在当前页加载")
            return page
        for np in new_pages:
            try:
                np.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass
            if is_editor(np.url):
                log(tag, "✅ 编辑器已在新标签页加载")
                return np
        dismiss_dialog(page, 2000)
        time.sleep(5)
    raise TimeoutError(f"{EDITOR_TIMEOUT_SEC}s 内编辑器未加载")


# ------------------------- 健康检查 -------------------------

def read_terminal_text(page) -> str:
    for frame in iter_frames(page):
        try:
            rows = frame.locator(".xterm-rows > div")
            n = rows.count()
            if n:
                return "\n".join(
                    rows.nth(i).inner_text() or "" for i in range(n)
                )
        except Exception:
            continue
    return ""


def ensure_terminal(page, tag: str):
    ta = find_in_frames(page, "textarea.xterm-helper-textarea", 20000)
    if not ta:
        log(tag, "未检测到终端，尝试创建（Ctrl+`）...")
        page.keyboard.press("Control+`")
        time.sleep(2)
        ta = find_in_frames(page, "textarea.xterm-helper-textarea", 15000)
    if not ta:
        click_if_found(page, ['button[title*="New Terminal"]'], 5000)
        ta = find_in_frames(page, "textarea.xterm-helper-textarea", 15000)
    if not ta:
        raise RuntimeError("无法定位终端（xterm textarea）")
    try:
        ta.click()
    except Exception:
        pass
    return ta


def paste_command(page, cmd: str, ta) -> bool:
    """向终端注入命令：剪贴板粘贴 -> insert_text -> 逐键输入，三层回退。"""
    # 1) 剪贴板粘贴
    try:
        origin = re.match(r"(https?://[^/]+)", page.url).group(1)
        page.context.grant_permissions(
            ["clipboard-read", "clipboard-write"], origin=origin
        )
        page.evaluate("c => navigator.clipboard.writeText(c)", cmd)
        page.keyboard.press("Control+v")
        page.keyboard.press("Enter")
        time.sleep(2)
        if "HEALTHCHECK-" in read_terminal_text(page):
            return True
    except Exception:
        pass
    # 2) insert_text
    try:
        ta.click()
        page.keyboard.insert_text(cmd)
        page.keyboard.press("Enter")
        time.sleep(2)
        if "HEALTHCHECK-" in read_terminal_text(page):
            return True
    except Exception:
        pass
    # 3) 逐键输入（慢速）
    try:
        ta.click()
        page.keyboard.type(cmd, delay=60)
        page.keyboard.press("Enter")
        time.sleep(2)
        return "HEALTHCHECK-" in read_terminal_text(page)
    except Exception:
        return False


def health_check(editor_page, tag: str) -> dict:
    """在 IDE 终端执行 supervisord 状态查询并断言（来自实战验证的脚本）。"""
    result = {"sv_pid": None, "marker": None, "tasks": {}, "raw": ""}

    log(tag, "⏳ 等待 .bashrc 钩子引导 supervisord ...")
    time.sleep(BOOTSTRAP_WAIT_SEC)
    dismiss_ide_notifications(editor_page)

    ta = ensure_terminal(editor_page, tag)
    cmd = HEALTH_CMD_TMPL.format(conf=SUPERVISOR_CONF)
    if not paste_command(editor_page, cmd, ta):
        result["raw"] = read_terminal_text(editor_page)
        raise RuntimeError("健康检查命令注入失败（终端无回显）")

    time.sleep(4)
    text = read_terminal_text(editor_page)
    result["raw"] = text

    m = re.search(r"SV-PID=(\d+)", text)
    result["sv_pid"] = int(m.group(1)) if m else None
    result["marker"] = "MARKER-EXISTS" in text

    for task in HEALTHCHECK_TASKS:
        m = re.search(rf"^{re.escape(task)}\s+RUNNING\s+pid\s+(\d+)", text, re.M)
        result["tasks"][task] = int(m.group(1)) if m else None

    # 自动修复：仅对未 RUNNING 的任务 start all（不影响已运行任务），复查一次
    dead = [t for t, pid in result["tasks"].items() if pid is None]
    if dead and AUTO_FIX and HEALTHCHECK_TASKS:
        log(tag, f"🔧 发现未运行任务 {dead}，执行 supervisorctl start all ...")
        fix_cmd = (
            f"supervisorctl -c {SUPERVISOR_CONF} start all >/dev/null 2>&1; "
            f"sleep 3; supervisorctl -c {SUPERVISOR_CONF} status"
        )
        paste_command(editor_page, fix_cmd, ta)
        time.sleep(8)
        text = read_terminal_text(editor_page)
        result["raw"] += "\n--- after autofix ---\n" + text
        for task in HEALTHCHECK_TASKS:
            m = re.search(rf"^{re.escape(task)}\s+RUNNING\s+pid\s+(\d+)", text, re.M)
            if m and result["tasks"][task] is None:
                result["tasks"][task] = int(m.group(1))

    return result


def report_health(tag: str, account: dict, r: dict) -> bool:
    ok_tasks = {t: p for t, p in r["tasks"].items() if p is not None}
    dead = [t for t, p in r["tasks"].items() if p is None] \
        if HEALTHCHECK_TASKS else []
    healthy = (not dead) and (r["sv_pid"] is not None) and r["marker"]

    log(tag, "--------- 健康检查结果 ---------")
    log(tag, f"supervisord pid : {r['sv_pid']}")
    log(tag, f"bootstrap 标记  : {'存在' if r['marker'] else '缺失'}")
    for t, p in r["tasks"].items():
        log(tag, f"  {'✅' if p else '❌'} {t:24s} {'RUNNING pid=' + str(p) if p else 'NOT RUNNING'}")
    log(tag, f"结论: {'✅ 应用运行正常' if healthy else '❌ 应用异常'}")

    gh_summary(
        f"### [{tag}] {account['space'] or account['user']}\n"
        f"| 项目 | 结果 |\n|---|---|\n"
        f"| supervisord pid | {r['sv_pid']} |\n"
        f"| bootstrap 标记 | {'✅' if r['marker'] else '❌'} |\n"
        + "".join(
            f"| {t} | {'✅ RUNNING pid=' + str(p) if p else '❌ NOT RUNNING'} |\n"
            for t, p in r["tasks"].items()
        )
        + f"\n**结论: {'✅ 应用运行正常' if healthy else '❌ 应用异常'}**\n"
    )
    return healthy


# --------------------------- 主流程 ---------------------------

def keepalive_one(account: dict, index: int) -> bool:
    tag = f"Account{index + 1}"
    log(tag, f"=== 开始保活 ({account['space'] or account['user']}) ===")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"),
            viewport={"width": 1600, "height": 1000},
        )
        page = context.new_page()
        active_page = page
        login_screenshot = None
        current_step = "初始化"
        try:
            current_step = "登录 BTP"
            login(page, account["bas_url"], account["user"], account["password"])
            dismiss_dialog(page, 10000)

            # 登录成功，截取控制台/工作空间列表
            try:
                login_screenshot = page.screenshot(full_page=False)
                log(tag, "📸 已截取登录成功控制台画面")
            except Exception as e:
                log(tag, f"截取登录成功图失败: {e}")

            current_step = "启动 dev space"
            start_space_if_stopped(page, tag)

            current_step = "进入工作区"
            editor_page = enter_workspace(page, context, account["space"], tag)
            if editor_page:
                active_page = editor_page

            health_summary = "未启用"
            if HEALTHCHECK_ENABLED:
                current_step = "应用健康检查"
                dismiss_dialog(editor_page, 5000)
                r = health_check(editor_page, tag)
                healthy = report_health(tag, account, r)
                ok_cnt = sum(1 for p in r["tasks"].values() if p is not None)
                health_summary = f"{'✅ 正常' if healthy else '❌ 异常'} ({ok_cnt}/{len(r['tasks'])} 任务运行)"
                log(tag, f"⏳ 停留 {STAY_SECONDS}s（记录活跃）...")
                time.sleep(STAY_SECONDS)
                log(tag, "✅ Done! Activity recorded.")
            else:
                healthy = True
                log(tag, f"⏳ 停留 {STAY_SECONDS}s（记录活跃）...")
                time.sleep(STAY_SECONDS)
                log(tag, "✅ Done! Activity recorded.")

            log(tag, f"=== 结束 {'✅' if healthy else '❌'} ===")

            # 优先使用编辑器/终端健康检查截图，若无则使用登录控制台截图
            final_screenshot = None
            try:
                final_screenshot = active_page.screenshot(full_page=False)
            except Exception:
                final_screenshot = login_screenshot

            send_telegram_notification(
                title=f"[{tag}] SAP BAS 保活成功",
                details=[
                    ("账号", account["user"]),
                    ("空间", account["space"] or "默认"),
                    ("登录状态", "✅ 登录成功"),
                    ("健康检查", health_summary),
                    ("活跃状态", f"已停留 {STAY_SECONDS} 秒"),
                ],
                photo_bytes=final_screenshot or login_screenshot,
                is_success=True,
            )
            return healthy

        except Exception as err:
            log(tag, f"❌ {err}")
            gh_summary(f"### [{tag}] ❌ 失败\n\n```\n{err}\n```\n")

            # 失败时立即截取当前活动页面现场
            fail_screenshot = None
            try:
                if active_page:
                    fail_screenshot = active_page.screenshot(full_page=False)
                    log(tag, "📸 已截取失败现场画面")
            except Exception as e:
                log(tag, f"截取失败现场图失败: {e}")

            send_telegram_notification(
                title=f"[{tag}] SAP BAS 保活失败",
                details=[
                    ("账号", account["user"]),
                    ("空间", account["space"] or "默认"),
                    ("失败步骤", current_step),
                    ("错误原因", str(err)[:200]),
                ],
                photo_bytes=fail_screenshot,
                is_success=False,
            )
            return False
        finally:
            try:
                browser.close()
            except Exception:
                pass


def main() -> int:
    accounts = parse_accounts()
    log("MAIN", f"🚀 开始保活，共 {len(accounts)} 个账号（每 2 小时由 GitHub Actions 触发）")
    results = []
    for i, acc in enumerate(accounts):
        try:
            results.append(keepalive_one(acc, i))
        except Exception as err:
            log(f"Account{i + 1}", f"❌ 未捕获异常: {err}")
            results.append(False)
        if i < len(accounts) - 1:
            time.sleep(5)

    ok = sum(results)
    log("MAIN", f"✅ 完成: {ok}/{len(results)} 个账号正常")
    gh_summary(f"---\n**汇总: {ok}/{len(results)} 个账号正常**\n")
    if not all(results) and FAIL_ON_UNHEALTHY:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
