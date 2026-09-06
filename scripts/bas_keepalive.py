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
    'echo HC_START_{nonce}; '
    'P=$(pgrep -x supervisord | head -1); echo SV_PID=$P; '
    'test -f /tmp/.sv-bootstrapped && echo MARKER=EXISTS || echo MARKER=MISSING; '
    'supervisorctl -c {conf} status 2>&1; '
    'echo HC_END_{nonce}'
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
    """处理 BAS 首次登录及 IDE 内各种弹窗（隐私声明、活动跟踪等）。"""
    selectors = [
        '.monaco-dialog-box .monaco-button:has-text("OK")',
        '.monaco-dialog-box [role="button"]:has-text("OK")',
        '.monaco-dialog-box a:has-text("OK")',
        'a.monaco-button:has-text("OK")',
        '[role="button"]:has-text("OK")',
        'button:has-text("OK")',
        'button:has-text("Accept")',
        'button:has-text("Agree")',
    ]
    for frame in iter_frames(page):
        try:
            for sel in selectors:
                cand = frame.locator(sel).first
                if cand.count() > 0 and cand.is_visible():
                    cand.click()
                    time.sleep(1)
                    log("DIALOG", f"已点击弹窗确认按钮 ({sel})")
                    return True
        except Exception:
            continue

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

    # 如果检测到模态遮罩，尝试 Escape 键关闭
    for frame in iter_frames(page):
        try:
            if frame.locator(".monaco-dialog-modal-block, .monaco-dialog-box").count() > 0:
                page.keyboard.press("Escape")
                time.sleep(1)
                log("DIALOG", "已按 Escape 尝试关闭弹窗遮罩")
                return True
        except Exception:
            continue

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

STATUS_RE = re.compile(r"^\s*(RUNNING|STOPPED|STARTING|STOPPING)\s*$", re.I)


def read_space_status(page, space_name: str = None) -> str:
    """读取 dev space 状态：严格精确匹配状态文本与标志位，杜绝误匹配页面提示横幅。"""
    for frame in iter_frames(page):
        # 1. 优先在包含 space_name 的卡片/行内查找精确状态
        if space_name:
            try:
                for container_sel in ["tr", "div.card", "div[class*='Card']", "div[class*='card']", "div[class*='devSpace']", "div[class*='dev-space']"]:
                    containers = frame.locator(container_sel)
                    for i in range(min(containers.count(), 10)):
                        c = containers.nth(i)
                        if c.get_by_text(space_name, exact=False).count() > 0:
                            for kw in ("STOPPED", "STARTING", "STOPPING", "RUNNING"):
                                m = c.get_by_text(re.compile(rf"^\s*{kw}\s*$", re.I))
                                if m.count() > 0 and m.first.is_visible():
                                    return kw
            except Exception:
                pass

        # 2. 精确匹配独立状态文本节点（严格 ^\s*STATUS\s*$，完全排斥包含 running 单词的长句子横幅）
        try:
            for kw in ("STOPPED", "STARTING", "STOPPING", "RUNNING"):
                loc = frame.get_by_text(re.compile(rf"^\s*{kw}\s*$", re.I))
                for i in range(min(loc.count(), 5)):
                    el = loc.nth(i)
                    if el.is_visible():
                        t = (el.inner_text() or "").strip().upper()
                        if t in ("RUNNING", "STOPPED", "STARTING", "STOPPING"):
                            return t
        except Exception:
            continue

    # 3. 回退：参考项目选择器与卡片超链接状态
    for frame in iter_frames(page):
        try:
            if frame.locator("a.stoppedStatus, .status-stopped, .stopped").count() > 0:
                return "STOPPED"
            if frame.locator("a.hyperlink:not(.disabled)[href*='#ws-']").count() > 0:
                return "RUNNING"
            if frame.locator("[aria-label*='Starting'], .starting").count() > 0:
                return "STARTING"
        except Exception:
            continue
    return "UNKNOWN"


def wait_dev_spaces_loaded(page, tag: str, timeout_sec: int = 45, space_name: str = None) -> str:
    """等待 BAS 空间控制台加载完毕，返回当前状态。"""
    log(tag, "⏳ 等待 dev space 列表加载完成 ...")
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        dismiss_dialog(page, 1500)
        status = read_space_status(page, space_name=space_name)
        if status in ("RUNNING", "STOPPED", "STARTING", "STOPPING"):
            return status
        time.sleep(2)
    return read_space_status(page, space_name=space_name)


def start_space_if_stopped(page, tag: str, initial_status: str = None, space_name: str = None) -> None:
    status = initial_status or wait_dev_spaces_loaded(page, tag, space_name=space_name)
    log(tag, f"📊 dev space 状态: {status}")

    if status == "RUNNING":
        return

    if status != "STARTING":
        log(tag, "▶️ dev space 未运行，点击启动 ...")
        started = click_if_found(
            page,
            [
                'button[title*="Start Dev Space"]',
                'button[aria-label*="Start Dev Space"]',
                'button[title*="Start"]',
                'button[aria-label*="Start"]',
                'button:has-text("Start")',
                "#startButton0",
                'a[title*="Start"]',
            ],
            6000,
        )
        if not started and space_name:
            # 尝试通过所属空间卡片查找启动按钮
            for frame in iter_frames(page):
                try:
                    for card_sel in ["tr", "div.card", "div[class*='Card']", "div[class*='card']", "div[class*='devSpace']", "div[class*='dev-space']"]:
                        cards = frame.locator(card_sel)
                        for i in range(min(cards.count(), 10)):
                            c = cards.nth(i)
                            if c.get_by_text(space_name, exact=False).count() > 0:
                                btn = c.locator('button[title*="Start"], button[aria-label*="Start"], button').first
                                if btn and btn.is_visible():
                                    btn.click()
                                    started = True
                                    log(tag, "已通过所属空间卡片点击启动按钮")
                                    break
                        if started:
                            break
                except Exception:
                    continue

        if not started:
            # 尝试通过卡片首个按钮点击启动
            for frame in iter_frames(page):
                try:
                    for card_sel in ['div.card', 'div[class*="Card"]', 'div[class*="dev-space"]']:
                        cards = frame.locator(card_sel)
                        if cards.count() > 0:
                            first_btn = cards.first.locator("button").first
                            if first_btn and first_btn.is_visible():
                                first_btn.click()
                                started = True
                                log(tag, "已通过卡片首个按钮点击启动")
                                break
                    if started:
                        break
                except Exception:
                    continue

        if not started:
            raise RuntimeError("未找到启动按钮（页面结构可能已变化）")
        log(tag, "✅ 已点击启动，等待 RUNNING ...")

    deadline = time.time() + START_TIMEOUT_SEC
    last_reload = time.time()
    while time.time() < deadline:
        time.sleep(5)
        dismiss_dialog(page, 1500)
        status = read_space_status(page, space_name=space_name)
        remaining = int(deadline - time.time())
        log(tag, f"   等待中... 剩余 {remaining}s，当前 {status}")
        if status == "RUNNING":
            log(tag, "✅ dev space 已 RUNNING")
            time.sleep(2)
            return

        # 仅当长时间（超过 60s）持续为 UNKNOWN 时才进行单次页面重载并等待重新渲染
        if status == "UNKNOWN" and (time.time() - last_reload > 60):
            log(tag, "⚠️ 状态读取异常，重新加载控制台页面...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=25000)
            except Exception:
                pass
            last_reload = time.time()
            wait_dev_spaces_loaded(page, tag, timeout_sec=25, space_name=space_name)

    raise TimeoutError(f"{START_TIMEOUT_SEC}s 内 dev space 未达到 RUNNING")


def enter_workspace(page, context, space: str, tag: str):
    """点击空间链接进入编辑器；编辑器可能在当前页（hash 路由）或新标签打开。"""
    log(tag, "🖱️ 进入 dev space 编辑器 ...")
    new_pages = []
    context.on("page", lambda p: new_pages.append(p))

    link = None
    link_deadline = time.time() + 15
    while time.time() < link_deadline:
        for frame in iter_frames(page):
            try:
                cand = frame.locator('a.hyperlink:not(.disabled)[href*="#ws-"]').first
                if cand.count() > 0 and cand.is_visible():
                    link = cand
                    break
                if space:
                    cand = frame.locator(
                        f'a.hyperlink:not(.disabled):has-text("{space}"), a:not(.disabled)[href*="#ws-"]:has-text("{space}")'
                    ).first
                    if cand.count() > 0 and cand.is_visible():
                        link = cand
                        break
                    cand = frame.locator(f'a:not(.disabled):has-text("{space}")').first
                    if cand.count() > 0 and cand.is_visible():
                        link = cand
                        break
            except Exception:
                continue
        if link:
            break
        time.sleep(2)

    if link:
        link.click()
    else:
        raise RuntimeError("未找到可点击的空间链接")

    time.sleep(3)

    def is_editor_url(url: str) -> bool:
        return "#ws-" in url

    target_page = None
    deadline = time.time() + EDITOR_TIMEOUT_SEC
    while time.time() < deadline:
        if is_editor_url(page.url):
            target_page = page
            break
        for np in new_pages:
            try:
                np.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass
            if is_editor_url(np.url):
                target_page = np
                break
        if target_page:
            break
        dismiss_dialog(page, 2000)
        time.sleep(3)

    if not target_page:
        raise TimeoutError(f"{EDITOR_TIMEOUT_SEC}s 内编辑器未加载（未跳转 #ws-）")

    log(tag, "⏳ 等待 IDE 工作区界面加载完成 ...")
    ide_ready = False
    ready_deadline = time.time() + EDITOR_TIMEOUT_SEC
    while time.time() < ready_deadline:
        dismiss_dialog(target_page, 2000)
        dismiss_ide_notifications(target_page)
        for frame in iter_frames(target_page):
            try:
                if (
                    frame.get_by_text("Terminal", exact=True).count() > 0
                    or frame.locator("textarea.xterm-helper-textarea").count() > 0
                    or frame.locator('div[role="menubar"]').count() > 0
                    or frame.locator(".theia-ApplicationShell").count() > 0
                    or frame.locator(".monaco-workbench").count() > 0
                ):
                    ide_ready = True
                    break
            except Exception:
                continue
        if ide_ready:
            break
        time.sleep(3)

    if ide_ready:
        log(tag, "✅ IDE 工作区已完全加载")
    else:
        log(tag, "⚠️ 达到超时未检测到标准 IDE 标记，继续尝试交互")

    return target_page


# ------------------------- 健康检查 -------------------------

def maximize_terminal(page, tag: str):
    """最大化终端窗口，便于展示全部命令及日志。"""
    try:
        # 先判断是否已是最大化状态
        for frame in iter_frames(page):
            try:
                is_max = frame.evaluate(
                    "() => !!document.querySelector('.part.panel.maximized, .panel.maximized')"
                )
                if is_max:
                    log(tag, "终端面板已处于最大化状态")
                    return True
            except Exception:
                pass

        max_btn = find_in_frames(
            page,
            'a[aria-label*="Toggle Maximize Panel"], a[aria-label*="Maximize Panel"], .codicon-chevron-up, button[title*="Toggle Maximize Panel"]',
            2000,
        )
        if max_btn:
            max_btn.click()
            log(tag, "✅ 已执行终端窗口最大化")
            time.sleep(1)
            return True
        page.keyboard.press("Control+Shift+Up")
        time.sleep(0.5)
    except Exception as e:
        log(tag, f"最大化终端面板跳过: {e}")
    return False


def focus_terminal(page, ta=None):
    """确保终端及辅助输入框获得焦点。"""
    for frame in iter_frames(page):
        try:
            ok = frame.evaluate(
                """() => {
                    const ta = document.querySelector('textarea.xterm-helper-textarea');
                    if (ta) { ta.focus(); return true; }
                    return false;
                }"""
            )
            if ok:
                break
        except Exception:
            pass

    for frame in iter_frames(page):
        try:
            loc = frame.locator('.xterm-screen, canvas.xterm-text-layer, .terminal-wrapper').first
            if loc and loc.is_visible():
                loc.click(force=True)
                break
        except Exception:
            pass


def _prepare_terminal(page, ta, tag: str):
    maximize_terminal(page, tag)
    focus_terminal(page, ta)
    return ta


def read_terminal_text(page, ta=None, tag="TERMINAL") -> str:
    """多策略读取终端输出：
    1. xterm 内存 Buffer 与 Selection 提取（穿透 Canvas 限制）
    2. DOM / Accessibility 文本读取（.xterm-rows / .xterm-accessibility-tree）
    3. VS Code 终端标签 OSC 0 标题状态提取
    """
    for frame in iter_frames(page):
        # 1. 尝试直接从 xterm 实例内存 Buffer 读取
        try:
            xterm_res = frame.evaluate(
                """() => {
                    const els = document.querySelectorAll('.terminal, .xterm, .terminal-wrapper, textarea.xterm-helper-textarea');
                    for (const el of els) {
                        for (const k of Object.keys(el)) {
                            const v = el[k];
                            if (!v || typeof v !== 'object') continue;
                            const term = v.buffer ? v : (v._terminal || v.terminal || v._xterm || v.xterm || v.raw);
                            if (term && term.buffer && term.buffer.active) {
                                const b = term.buffer.active;
                                const lines = [];
                                for (let i = 0; i < b.length; i++) {
                                    const l = b.getLine(i);
                                    if (l) lines.push(l.translateToString(true));
                                }
                                const res = lines.join('\\n').trim();
                                if (res) {
                                    return res;
                                }
                            }
                        }
                    }
                    return '';
                }"""
            )
            if xterm_res:
                return xterm_res.replace('\xa0', ' ')
        except Exception:
            pass

        # 2. 尝试从 DOM / Accessibility 提取
        try:
            dom_text = frame.evaluate(
                """() => {
                    const selectors = [
                        '.xterm-rows > div',
                        '.xterm-accessibility-tree div',
                        '[class*="xterm-accessibility"] div',
                        'div[role="document"] div'
                    ];
                    for (const sel of selectors) {
                        const els = document.querySelectorAll(sel);
                        if (els && els.length > 0) {
                            const str = Array.from(els).map(l => l.textContent || '').join('\\n').trim();
                            if (str) {
                                return str;
                            }
                        }
                    }
                    return '';
                }"""
            )
            if dom_text:
                return dom_text.replace('\xa0', ' ')
        except Exception:
            pass

    return ""


def ensure_terminal(page, tag: str):
    dismiss_dialog(page, 2000)
    dismiss_ide_notifications(page)

    # 1. 若已有终端输入框，直接返回
    ta = find_in_frames(page, "textarea.xterm-helper-textarea", 3000)
    if ta:
        return _prepare_terminal(page, ta, tag)

    log(tag, "未检测到现有终端，尝试通过菜单与命令面板创建 ...")

    # 2. 策略一：点击顶部左侧汉堡菜单（≡）-> Terminal -> New Terminal
    try:
        menu_clicked = False
        menu_btn = find_in_frames(
            page,
            '[aria-label*="Application Menu"], .menubar-menu-button, .codicon-menu, div.titlebar-left [role="button"]',
            3000,
        )
        if menu_btn:
            menu_btn.click()
            menu_clicked = True
        else:
            page.mouse.click(15, 18)
            menu_clicked = True

        if menu_clicked:
            time.sleep(1)
            term_item = find_in_frames(
                page,
                '[role="menuitem"]:has-text("Terminal"), .monaco-action-bar :text("Terminal"), span:has-text("Terminal")',
                3000,
            )
            if term_item:
                term_item.click()
                time.sleep(1)
                new_term = find_in_frames(
                    page,
                    '[role="menuitem"]:has-text("New Terminal"), span:has-text("New Terminal")',
                    3000,
                )
                if new_term:
                    new_term.click()
                    log(tag, "✅ 已通过 ≡ 应用菜单触发 New Terminal")
                    time.sleep(4)
    except Exception as e:
        log(tag, f"菜单触发终端异常: {e}")

    ta = find_in_frames(page, "textarea.xterm-helper-textarea", 5000)
    if ta:
        return _prepare_terminal(page, ta, tag)

    # 3. 策略二：利用顶部 Command Palette 命令面板
    try:
        page.keyboard.press("F1")
        time.sleep(1)
        cmd_input = find_in_frames(page, "input.quick-input-input", 2000)
        if not cmd_input:
            search_box = find_in_frames(
                page,
                'input[placeholder*="Search"], div.search-label, .quick-input-widget',
                2000,
            )
            if search_box:
                search_box.click()
            else:
                page.mouse.click(500, 18)
            time.sleep(1)
            cmd_input = find_in_frames(page, "input.quick-input-input", 3000)

        if cmd_input:
            cmd_input.fill(">Terminal: Create New Terminal")
            time.sleep(1)
            page.keyboard.press("Enter")
            time.sleep(4)
    except Exception as e:
        log(tag, f"命令面板触发终端异常: {e}")

    ta = find_in_frames(page, "textarea.xterm-helper-textarea", 5000)
    if ta:
        return _prepare_terminal(page, ta, tag)

    # 4. 策略三：界面快捷键兜底
    for key in ("Control+Backquote", "Control+grave", "Control+Shift+Backquote"):
        try:
            page.keyboard.press(key)
            time.sleep(2)
            ta = find_in_frames(page, "textarea.xterm-helper-textarea", 4000)
            if ta:
                return _prepare_terminal(page, ta, tag)
        except Exception:
            pass

    # 5. 策略四：面板切换按钮匹配
    click_if_found(
        page,
        [
            'button[title*="Toggle Panel"]',
            'button[aria-label*="Toggle Panel"]',
            '.codicon-layout-panel-bottom',
            '.codicon-layout-panel',
            'button[title*="New Terminal"]',
            'a[title*="New Terminal"]',
        ],
        4000,
    )

    ta = find_in_frames(page, "textarea.xterm-helper-textarea", 10000)
    if not ta:
        raise RuntimeError("无法定位终端（xterm textarea）")
    return _prepare_terminal(page, ta, tag)


def paste_command(page, cmd: str, ta=None) -> bool:
    """向终端注入命令并回车执行。"""
    focus_terminal(page, ta)
    time.sleep(0.3)
    page.keyboard.press("Control+c")
    time.sleep(0.5)

    injected = False
    # 策略一：直接通过 xterm.js 实例执行 paste
    for frame in iter_frames(page):
        try:
            injected = frame.evaluate(
                """(command) => {
                    const els = document.querySelectorAll('.terminal, .xterm, .terminal-wrapper, textarea.xterm-helper-textarea');
                    for (const el of els) {
                        for (const k of Object.keys(el)) {
                            const v = el[k];
                            if (!v || typeof v !== 'object') continue;
                            const term = v.buffer ? v : (v._terminal || v.terminal || v._xterm || v.xterm || v.raw);
                            if (term) {
                                if (typeof term.focus === 'function') term.focus();
                                if (typeof term.paste === 'function') {
                                    term.paste(command + '\\r');
                                    return true;
                                }
                            }
                        }
                    }
                    return false;
                }""",
                cmd,
            )
            if injected:
                time.sleep(0.5)
                break
        except Exception:
            pass

    if not injected:
        # 策略二：剪贴板写入并粘贴
        try:
            origin = re.match(r"(https?://[^/]+)", page.url).group(1)
            page.context.grant_permissions(
                ["clipboard-read", "clipboard-write"], origin=origin
            )
            page.evaluate("c => navigator.clipboard.writeText(c)", cmd)
            focus_terminal(page, ta)
            page.keyboard.press("Control+v")
            time.sleep(0.3)
            page.keyboard.press("Enter")
            injected = True
        except Exception as e:
            log("TERMINAL", f"剪贴板操作异常: {e}")
            try:
                focus_terminal(page, ta)
                page.keyboard.insert_text(cmd)
                time.sleep(0.3)
                page.keyboard.press("Enter")
                injected = True
            except Exception:
                pass

    focus_terminal(page, ta)
    page.keyboard.press("Enter")
    return injected


def health_check(editor_page, tag: str) -> dict:
    """在 IDE 终端执行 supervisord 状态查询并断言。"""
    result = {
        "sv_pid": None,
        "marker": None,
        "tasks": {t: None for t in HEALTHCHECK_TASKS},
        "raw": "",
    }

    log(tag, "⏳ 等待 .bashrc 钩子引导 supervisord ...")
    time.sleep(BOOTSTRAP_WAIT_SEC)
    dismiss_ide_notifications(editor_page)

    ta = ensure_terminal(editor_page, tag)
    nonce = uuid.uuid4().hex[:8]
    start_tag = f"HC_START_{nonce}"
    end_tag = f"HC_END_{nonce}"

    cmd = HEALTH_CMD_TMPL.format(conf=SUPERVISOR_CONF, nonce=nonce)
    log(tag, f"注入健康检查命令 (nonce={nonce}) ...")
    if not paste_command(editor_page, cmd, ta):
        raise RuntimeError("终端命令注入失败")

    # 轮询等待健康检查命令在 bash 中执行并生成完整回显
    log(tag, "⏳ 等待健康检查命令输出 ...")
    text = ""
    deadline = time.time() + 30
    raw_text = ""
    while time.time() < deadline:
        time.sleep(2.5)
        raw_text = read_terminal_text(editor_page, ta=ta, tag=tag)
        m_span = re.search(
            rf"(?:^|[\r\n]+){re.escape(start_tag)}\s*[\r\n]+(.*?)[\r\n]+{re.escape(end_tag)}",
            raw_text,
            re.DOTALL,
        )
        if m_span:
            text = m_span.group(1).strip()
            log(tag, "✅ 已捕获当前命令完整回显区间")
            break

    result["raw"] = text or raw_text
    if not text:
        raise RuntimeError("健康检查命令未生成有效回显（未能定位执行标识）")

    m = re.search(r"SV_PID=(\d+)", text) or re.search(r"SV-PID=(\d+)", text) or re.search(r"PID=(\d+)", text)
    result["sv_pid"] = int(m.group(1)) if m else None
    result["marker"] = ("MARKER=EXISTS" in text) or ("MARKER-EXISTS" in text)

    for task in HEALTHCHECK_TASKS:
        m_task = re.search(rf"^{re.escape(task)}\s+RUNNING\s+pid\s+(\d+)", text, re.M)
        if m_task:
            result["tasks"][task] = int(m_task.group(1))
        else:
            m_alt = re.search(rf"{re.escape(task)}\s+RUNNING\s+pid\s*(\d+)", text)
            if m_alt:
                result["tasks"][task] = int(m_alt.group(1))

    # 自动修复：仅对未 RUNNING 的任务 start all（不影响已运行任务），复查一次
    dead = [t for t, pid in result["tasks"].items() if pid is None]
    if dead and AUTO_FIX and HEALTHCHECK_TASKS:
        log(tag, f"🔧 发现未运行任务 {dead}，执行 supervisorctl start all ...")
        fix_nonce = uuid.uuid4().hex[:8]
        fix_start = f"FIX_START_{fix_nonce}"
        fix_end = f"FIX_END_{fix_nonce}"
        fix_cmd = (
            f"echo {fix_start}; "
            f"supervisorctl -c {SUPERVISOR_CONF} start all >/dev/null 2>&1; "
            f"sleep 3; supervisorctl -c {SUPERVISOR_CONF} status 2>&1; "
            f"echo {fix_end}"
        )
        paste_command(editor_page, fix_cmd, ta)
        fix_deadline = time.time() + 20
        fix_text = ""
        while time.time() < fix_deadline:
            time.sleep(2.5)
            r_text = read_terminal_text(editor_page, ta=ta, tag=tag)
            m_fix = re.search(
                rf"(?:^|[\r\n]+){re.escape(fix_start)}\s*[\r\n]+(.*?)[\r\n]+{re.escape(fix_end)}",
                r_text,
                re.DOTALL,
            )
            if m_fix:
                fix_text = m_fix.group(1).strip()
                break
        result["raw"] += "\n--- after autofix ---\n" + (fix_text or r_text)
        for task in HEALTHCHECK_TASKS:
            m = re.search(rf"^{re.escape(task)}\s+RUNNING\s+pid\s+(\d+)", result["raw"], re.M)
            if m and result["tasks"][task] is None:
                result["tasks"][task] = int(m.group(1))
            else:
                m_alt = re.search(rf"{re.escape(task)}\s+RUNNING\s+pid\s*(\d+)", result["raw"])
                if m_alt and result["tasks"][task] is None:
                    result["tasks"][task] = int(m_alt.group(1))

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

            # 等待空间卡片加载完成，确保截取到完整的空间卡片画面
            space_name = account.get("space")
            status = wait_dev_spaces_loaded(page, tag, space_name=space_name)

            # 登录成功，截取控制台/工作空间列表
            try:
                login_screenshot = page.screenshot(full_page=False)
                log(tag, "📸 已截取登录成功控制台画面")
                os.makedirs("screenshots", exist_ok=True)
                with open(f"screenshots/{tag}_login.png", "wb") as f:
                    f.write(login_screenshot)
            except Exception as e:
                log(tag, f"截取登录成功图失败: {e}")

            current_step = "启动 dev space"
            start_space_if_stopped(page, tag, initial_status=status, space_name=space_name)

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
                dismiss_dialog(active_page, 1000)
                time.sleep(1)
                final_screenshot = active_page.screenshot(full_page=False)
            except Exception:
                final_screenshot = login_screenshot

            if final_screenshot or login_screenshot:
                try:
                    os.makedirs("screenshots", exist_ok=True)
                    with open(f"screenshots/{tag}_success.png", "wb") as f:
                        f.write(final_screenshot or login_screenshot)
                except Exception:
                    pass

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
                    os.makedirs("screenshots", exist_ok=True)
                    with open(f"screenshots/{tag}_failed.png", "wb") as f:
                        f.write(fail_screenshot)
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
