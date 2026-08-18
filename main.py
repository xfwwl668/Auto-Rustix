#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rustix 服务器自动启动脚本（UC 版）
- 支持多账号轮流操作
- 自动登录 https://my.rustix.me/auth/login
- 使用 seleniumbase UC Mode 绕过 Mitelis DDoS 防护 / JS 挑战
- 点击 Manage Server -> 判断 start 按钮状态 -> 启动服务器
- 监听页面状态 / 控制台 "Running Done!" 确认上线
- 通过 stop 按钮可点击状态验证（不点击 stop）

站点语言：俄语 / 英语（不支持中文）

参考：借鉴 Wispbyte 项目的 UC Mode 登录与 Turnstile 处理模式。
"""

import json
import os
import sys
import time
import logging
import argparse
from urllib.parse import unquote, urlsplit

from seleniumbase import SB
from seleniumbase.common.exceptions import TimeoutException

import notify

# ---------------- 日志配置 ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("run.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("rustix-auto")

LOGIN_URL = "https://my.rustix.me/auth/login"
HOME_URL = "https://my.rustix.me"
START_WAIT_TIMEOUT = 180
LOGIN_WAIT_TIMEOUT = 30
POST_LOGIN_WAIT = 30

# 登录表单选择器（按优先级尝试）
EMAIL_SELECTORS = [
    'input[type="email"]',
    'input[name="email"]',
    'input[autocomplete="email"]',
    'input#email',
    'input[placeholder*="mail" i]',
    'input[placeholder*="почт" i]',
]
PASSWORD_SELECTORS = [
    'input[type="password"]',
    'input[name="password"]',
    'input[autocomplete="current-password"]',
    'input#password',
    'input[placeholder*="password" i]',
    'input[placeholder*="парол" i]',
]
SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Login")',
    'button:has-text("Sign in")',
    'button:has-text("Войти")',
    'button:has-text("Вход")',
    'button:has-text("Log in")',
]

# Manage Server 按钮
MANAGE_SELECTORS = [
    'a:has-text("Manage Server")',
    'button:has-text("Manage Server")',
    'text=Manage Server',
    'a:has-text("Управление")',
    'a:has-text("Manage")',
]

# Start 按钮
START_BUTTON_SELECTORS = [
    'button:has-text("Start")',
    'button:has-text("Запустить")',
    'button:has-text("Запуск")',
    '[aria-label*="start" i]',
    'button:has-text("Start Server")',
    'button:has-text("Power On")',
]

# Stop 按钮（用于验证在线状态，不点击）
STOP_BUTTON_SELECTORS = [
    'button:has-text("Stop")',
    'button:has-text("Остановить")',
    'button:has-text("Остановка")',
    '[aria-label*="stop" i]',
]


def parse_accounts_string(raw: str):
    accounts = []
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        email, password = item.split(":", 1)
        email, password = email.strip(), password.strip()
        if email and password:
            accounts.append({"email": email, "password": password})
    return accounts


def load_accounts():
    accounts_env = os.environ.get("ACCOUNTS", "").strip()
    if accounts_env:
        accounts = parse_accounts_string(accounts_env)
        if accounts:
            logger.info(f"从环境变量 ACCOUNTS 加载到 {len(accounts)} 个账号")
            return accounts

    accounts_file = os.environ.get("ACCOUNTS_FILE", "accounts.json")
    if os.path.exists(accounts_file):
        with open(accounts_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("accounts"), list):
            return data["accounts"]
    logger.error("未找到账号配置")
    return []


def first_visible(sb, selectors, timeout=1000):
    """依次尝试选择器，返回第一个可见元素；找不到返回 None。"""
    for selector in selectors:
        try:
            if sb.is_element_visible(selector, timeout=timeout / 1000.0):
                return selector
        except Exception:
            pass
    return None


def is_clickable(sb, selector):
    try:
        if not sb.is_element_visible(selector, timeout=2):
            return False
        disabled = sb.get_attribute(selector, "disabled")
        if disabled:
            return False
        aria = sb.get_attribute(selector, "aria-disabled")
        if aria and aria.lower() == "true":
            return False
        cls = sb.get_attribute(selector, "class") or ""
        if "disabled" in cls.lower():
            return False
        return True
    except Exception:
        return False


def check_turnstile_solved(sb) -> bool:
    """检查当前页面/弹窗中的 Turnstile 是否已完成"""
    try:
        return bool(sb.execute_script('''
            var inp = document.querySelector('input[name="cf-turnstile-response"]');
            if (inp && inp.value && inp.value.length > 20) return true;
            var iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            if (iframe && iframe.getAttribute("data-state") === "solved") return true;
            var success = document.getElementById('success');
            return !!(success && getComputedStyle(success).display !== 'none');
        '''))
    except Exception:
        return False


def wait_for_turnstile_success(sb, timeout: int = 35) -> bool:
    """等待并点击登录页 Turnstile（借鉴 Wispbyte）"""
    logger.info("等待 Turnstile 验证...")
    start = time.time()
    last_click = 0
    while time.time() - start < timeout:
        if check_turnstile_solved(sb):
            logger.info("✅ Turnstile 验证成功")
            return True
        if time.time() - last_click > 3:
            try:
                sb.uc_gui_click_captcha()
                last_click = time.time()
                logger.info("点击 Turnstile")
            except Exception as e:
                logger.warning(f"点击 Turnstile 异常: {e}")
        time.sleep(1)
    logger.warning("⏰ Turnstile 验证超时")
    return False


def page_is_ready(sb) -> bool:
    """检查页面是否已加载出真实内容（而非防护挑战页/空白页）"""
    try:
        url = sb.get_current_url()
        text = sb.execute_script("return document.body ? document.body.innerText : ''") or ""
        html = sb.execute_script("return document.documentElement ? document.documentElement.outerHTML : ''") or ""
        # 如果页面里只有防护脚本特征，视为未就绪
        if "challenge" in html.lower() and "FsGtA7wj4k6YkizM" in html:
            return False
        if len(text.strip()) < 5 and "input" not in html.lower():
            return False
        return True
    except Exception:
        return False


def take_debug_screenshot(sb, tag: str, email: str = "") -> str:
    """保存调试截图，返回文件名"""
    try:
        safe_email = email.replace("@", "_at_").replace(".", "_") if email else "unknown"
        fname = f"debug_{tag}_{safe_email}.png"
        sb.save_screenshot(fname)
        logger.info(f"已保存调试截图: {fname}")
        return fname
    except Exception as e:
        logger.warning(f"截图失败: {e}")
        return ""


def open_login_with_retry(sb, max_retries: int = 3):
    """使用 UC 模式打开登录页，处理防护挑战（借鉴 Wispbyte uc_open_with_reconnect）"""
    for attempt in range(1, max_retries + 1):
        logger.info(f"打开登录页 (第 {attempt}/{max_retries} 次): {LOGIN_URL}")
        try:
            sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=10)
        except Exception as e:
            logger.warning(f"uc_open 异常: {e}")
        time.sleep(4)
        if page_is_ready(sb):
            return True
        logger.warning(f"第 {attempt} 次打开后页面仍不可用，重试...")
        time.sleep(3)
    return False


def fill_login_form(sb, email: str, password: str):
    """填写并提交登录表单，返回是否提交成功"""
    email_sel = first_visible(sb, EMAIL_SELECTORS)
    password_sel = first_visible(sb, PASSWORD_SELECTORS)
    if not email_sel or not password_sel:
        logger.error("找不到登录表单 (email/password 输入框)")
        return False
    logger.info(f"找到登录表单: email={email_sel}, password={password_sel}")
    sb.type(email_sel, email)
    time.sleep(0.5)
    sb.type(password_sel, password)
    time.sleep(0.5)

    wait_for_turnstile_success(sb, timeout=35)

    submit_sel = first_visible(sb, SUBMIT_SELECTORS)
    if submit_sel:
        logger.info(f"点击登录按钮: {submit_sel}")
        try:
            sb.click(submit_sel)
        except Exception as e:
            logger.warning(f"点击登录按钮失败: {e}，尝试 Enter 提交")
            sb.press(password_sel, "Enter")
    else:
        logger.info("未找到登录按钮，尝试 Enter 提交")
        sb.press(password_sel, "Enter")
    return True


def login(sb, email: str, password: str) -> bool:
    """完整登录流程，返回是否登录成功"""
    if not open_login_with_retry(sb):
        logger.error("登录页始终无法加载（防护挑战未通过）")
        take_debug_screenshot(sb, "login", email)
        return False

    if not fill_login_form(sb, email, password):
        take_debug_screenshot(sb, "login", email)
        return False

    logger.info("等待跳转到仪表盘...")
    # 登录成功 = 已离开登录页，且页面出现登录态标志
    login_ok = False
    for _ in range(POST_LOGIN_WAIT):
        url = sb.get_current_url()
        if "login" in url.lower() or "auth" in url.lower():
            time.sleep(1)
            continue
        try:
            body = sb.execute_script(
                "return (document.body ? document.body.innerText : '') + ' ' + document.title"
            ) or ""
            low = body.lower()
            if any(k in low for k in ["manage server", "your servers", "welcome", "dashboard",
                                      "console", "control panel", "server list",
                                      "управление", "панель", "сервер"]):
                login_ok = True
                break
        except Exception:
            pass
        time.sleep(1)

    if not login_ok:
        fail_url = sb.get_current_url()
        logger.error(f"登录后未成功跳转（当前 URL: {fail_url[:100]}）")
        take_debug_screenshot(sb, "login_fail", email)
        try:
            page_text = sb.execute_script("return document.body ? document.body.innerText : ''") or ""
            if "invalid email or password" in page_text.lower() or "incorrect" in page_text.lower():
                logger.error("⚠️ 平台提示账号密码错误 —— 检查 RUSTIX_ACCOUNTS Secret")
            elif "turnstile" in page_text.lower() or "verify you are human" in page_text.lower():
                logger.warning("⚠️ 页面出现人机验证未通过提示")
        except Exception:
            pass
        return False

    logger.info("✅ 登录成功并进入仪表盘")
    return True


def click_manage_server(sb) -> bool:
    logger.info("寻找 Manage Server 按钮...")
    sel = first_visible(sb, MANAGE_SELECTORS)
    if not sel:
        logger.error("未找到 Manage Server 按钮")
        return False
    logger.info(f"点击: {sel}")
    sb.click(sel)
    time.sleep(5)
    return True


def page_contains_text(sb, keywords) -> bool:
    """检查页面 DOM 文本是否包含任一关键词（用于控制台输出检测）"""
    try:
        text = sb.execute_script(
            "return document.body ? document.body.innerText : ''"
        ) or ""
        low = text.lower()
        return any(k in low for k in keywords)
    except Exception:
        return False


def start_server(sb, console_lines: list) -> str:
    """检测并启动服务器，返回状态: started / online / offline / no_start / unknown"""
    start_sel = first_visible(sb, START_BUTTON_SELECTORS)
    if not start_sel:
        # 没有 Start 按钮：可能已在线（Stop 按钮可见且可点击）
        stop_sel = first_visible(sb, STOP_BUTTON_SELECTORS)
        if stop_sel and is_clickable(sb, stop_sel):
            logger.info("未找到 Start 按钮但 Stop 可点击 → 服务器在线")
            return "online"
        # 页面文本已显示运行中
        if page_contains_text(sb, ["running", "online", "active", "работает", "запущен"]):
            logger.info("页面文本显示服务器运行中")
            return "online"
        logger.warning("未找到 Start/Stop 按钮")
        return "no_start"

    if not is_clickable(sb, start_sel):
        logger.info("Start 按钮存在但不可点击 → 服务器可能在线")
        return "online"

    logger.info("点击 Start 按钮...")
    sb.click(start_sel)
    logger.info("已点击 Start，等待服务器上线...")
    deadline = time.time() + START_WAIT_TIMEOUT
    while time.time() < deadline:
        # 控制台消息检查（浏览器 console）
        for line in console_lines:
            if "running done" in line.lower():
                logger.info("✅ 控制台输出 Running Done!")
                return "started"
        # 页面 DOM 文本检查（Pterodactyl 面板控制台）
        if page_contains_text(sb, ["running done", "server running", "started successfully"]):
            logger.info("✅ 页面控制台显示 Running Done!")
            return "started"
        # Start 按钮变为不可点击 / Stop 按钮变为可点击 = 已启动
        if not is_clickable(sb, start_sel):
            stop_sel = first_visible(sb, STOP_BUTTON_SELECTORS)
            if stop_sel and is_clickable(sb, stop_sel):
                logger.info("✅ Start 按钮失效且 Stop 可点击 → 服务器已上线")
                return "started"
        time.sleep(3)
    logger.warning("等待服务器上线超时")
    return "offline"


def process_account(account: dict, proxy_config: str = None) -> dict:
    email = account.get("email", "").strip()
    password = account.get("password", "").strip()
    result = {"email": email, "ok": False, "status": "unknown", "error": ""}
    if not email or not password:
        result["error"] = "账号或密码为空"
        return result

    try:
        if proxy_config:
            parsed = urlsplit(proxy_config)
            scheme = parsed.scheme.lower()
            if scheme == "socks":
                scheme = "socks5"
            if scheme not in {"http", "https", "socks5"} or not parsed.hostname or not parsed.port:
                raise RuntimeError("代理必须是有效的 http(s):// 或 socks5:// 地址")
            proxy_config = f"{scheme}://{parsed.hostname}:{parsed.port}"
            logger.info(f"已启用代理: {proxy_config}")

    except Exception as e:
        result["error"] = f"代理配置错误: {e}"
        return result

    try:
        sb_kwargs = dict(
            uc=True,
            test=True,
            locale="en",
            headed=False,
            chromium_arg="--disable-blink-features=AutomationControlled",
        )
        if proxy_config:
            sb_kwargs["proxy"] = proxy_config
        with SB(**sb_kwargs) as sb:
            # 监听控制台消息（通过 CDP Runtime.consoleAPICalled）
            console_lines = []
            def on_console_msg(msg):
                try:
                    text = msg.get("params", {}).get("args", [{}])[0].get("value", "") if isinstance(msg, dict) else str(msg)
                    if not text:
                        text = str(msg)
                except Exception:
                    text = str(msg)
                console_lines.append(text)
                if any(k in text.lower() for k in ["app is running", "running done", "started", "error"]):
                    logger.info(f"[console] {text[:200]}")
            try:
                driver = sb.driver
                driver.execute_cdp_cmd("Runtime.enable", {})
                driver.execute_cdp_cmd("Log.enable", {})
                # selenium 的 DevToolsListener
                try:
                    driver.add_listener("Log.entryAdded", lambda ev: on_console_msg(ev))
                except Exception:
                    pass
                # 轮询 consoleAPICalled 的方式在 uc 模式下不可靠，靠页面文本兜底
            except Exception:
                pass

            if not login(sb, email, password):
                result["error"] = "登录失败"
                return result

            if not click_manage_server(sb):
                result["error"] = "未找到 Manage Server"
                return result

            result["status"] = start_server(sb, console_lines)
            result["ok"] = result["status"] in {"started", "online"}
            if not result["ok"]:
                result["error"] = "服务器未成功启动"
            return result
    except Exception as e:
        result["error"] = f"运行异常: {type(e).__name__}: {e}"
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--proxy", default="", help="代理地址，如 socks5://127.0.0.1:1080（优先级高于 PROXY_URL 环境变量）")
    args = parser.parse_args()
    accounts = load_accounts()
    if not accounts:
        sys.exit(1)
    logger.info(f"共加载 {len(accounts)} 个账号")
    proxy_config = args.proxy.strip() or os.environ.get("PROXY_URL", "").strip()
    results = []
    for index, account in enumerate(accounts, 1):
        logger.info(f"--- 第 {index}/{len(accounts)} 个账号 ---")
        results.append(process_account(account, proxy_config))
    logger.info("================ 结果汇总 ================")
    ok = 0
    for result in results:
        logger.info(f"[{'OK' if result['ok'] else 'FAIL'}] {result['email']} | status={result['status']} | {result['error']}")
        if result["ok"]:
            ok += 1
    logger.info(f"成功 {ok}/{len(results)}")
    if notify.tg_enabled():
        notify.notify_summary(results)
    sys.exit(0 if ok == len(results) and ok > 0 else 1)


if __name__ == "__main__":
    main()
