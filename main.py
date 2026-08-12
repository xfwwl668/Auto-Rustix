#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rustix 服务器自动启动脚本
- 支持多账号轮流操作
- 自动登录 https://my.rustix.me/auth/login
- 点击 Manage Server -> 判断 start 按钮状态 -> 启动服务器
- 监听浏览器控制台 "Running Done!" 确认上线
- 通过 stop 按钮可点击状态验证（不点击 stop）

站点语言：俄语 / 英语（不支持中文）
"""

import json
import os
import sys
import time
import logging
import argparse
from datetime import datetime
from urllib.parse import unquote, urlsplit

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout
from playwright_stealth import stealth_sync

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
START_WAIT_TIMEOUT = 120
STEP_WAIT = 3000
LOGIN_PAGE_WAIT = 6000


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


def first_visible(page: Page, selectors):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=1000):
                return locator
        except Exception:
            pass
    return None


def fill_login_form(page: Page, email: str, password: str):
    email_input = first_visible(page, [
        'input[type="email"]',
        'input[name="email"]',
        'input[autocomplete="email"]',
        'input[placeholder*="mail" i]',
    ])
    password_input = first_visible(page, [
        'input[type="password"]',
        'input[name="password"]',
        'input[autocomplete="current-password"]',
    ])
    if not email_input or not password_input:
        return False
    email_input.fill(email)
    password_input.fill(password)
    submit = first_visible(page, [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Login")',
        'button:has-text("Sign in")',
        'button:has-text("Войти")',
    ])
    if not submit:
        password_input.press("Enter")
    else:
        submit.click()
    return True


def is_clickable(locator):
    try:
        return locator.is_visible() and locator.is_enabled()
    except Exception:
        return False


def click_manage_server(page: Page):
    button = first_visible(page, [
        'a:has-text("Manage Server")',
        'button:has-text("Manage Server")',
        'text=Manage Server',
    ])
    if not button:
        return False
    button.click()
    page.wait_for_timeout(STEP_WAIT)
    return True


def start_server(page: Page, console_lines: list):
    start_btn = first_visible(page, [
        'button:has-text("Start")',
        'button:has-text("Запустить")',
        '[aria-label*="start" i]',
    ])
    if not start_btn:
        return "unknown"
    if not is_clickable(start_btn):
        return "online"
    start_btn.click()
    deadline = time.time() + START_WAIT_TIMEOUT
    while time.time() < deadline:
        if any("running done" in line.lower() for line in console_lines):
            return "started"
        page.wait_for_timeout(1000)
    return "offline"


def process_account(account: dict, playwright, headless: bool = True) -> dict:
    email = account.get("email", "").strip()
    password = account.get("password", "").strip()
    result = {"email": email, "ok": False, "status": "unknown", "error": ""}
    if not email or not password:
        result["error"] = "账号或密码为空"
        return result
    browser = None
    try:
        raw_proxy = os.environ.get("PROXY_URL", "").strip()
        launch_options = {
            "headless": headless,
            "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        }
        if raw_proxy:
            parsed = urlsplit(raw_proxy)
            scheme = parsed.scheme.lower()
            if scheme == "socks":
                scheme = "socks5"
            if scheme not in {"http", "https", "socks5"} or not parsed.hostname or not parsed.port:
                raise RuntimeError("PROXY_URL 必须是有效的 http(s):// 或 socks5:// 地址")
            launch_options["proxy"] = {
                "server": f"{scheme}://{parsed.hostname}:{parsed.port}",
                **({"username": unquote(parsed.username)} if parsed.username else {}),
                **({"password": unquote(parsed.password)} if parsed.password else {}),
            }
            logger.info(f"已启用代理: {scheme}://{parsed.hostname}:{parsed.port}")
        browser = playwright.chromium.launch(**launch_options)
        context = browser.new_context(
            viewport={"width": 1366, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            locale="en-US",
        )
        page = context.new_page()
        stealth_sync(page)
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        console_lines = []
        def on_console(msg):
            text = msg.text or ""
            console_lines.append(text)
            if any(k in text.lower() for k in ["app is running", "error", "started", "running"]):
                logger.info(f"[console] {text}")
        page.on("console", on_console)
        logger.info(f"打开登录页: {LOGIN_URL}")
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(LOGIN_PAGE_WAIT)
        page.screenshot(path=f"debug_login_{email.replace('@', '_at_')}.png", full_page=True)
        if not fill_login_form(page, email, password):
            result["error"] = "找不到登录表单"
            return result
        page.wait_for_timeout(STEP_WAIT)
        if "/login" in page.url:
            result["error"] = "登录失败"
            return result
        if not click_manage_server(page):
            result["error"] = "未找到 Manage Server"
            return result
        result["status"] = start_server(page, console_lines)
        result["ok"] = result["status"] in {"started", "online"}
        if not result["ok"]:
            result["error"] = "服务器未成功启动"
        return result
    except PWTimeout as e:
        result["error"] = f"页面超时: {e}"
        return result
    except Exception as e:
        result["error"] = f"运行异常: {e}"
        return result
    finally:
        if browser:
            browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    accounts = load_accounts()
    if not accounts:
        sys.exit(1)
    logger.info(f"共加载 {len(accounts)} 个账号")
    results = []
    with sync_playwright() as playwright:
        for index, account in enumerate(accounts, 1):
            logger.info(f"--- 第 {index}/{len(accounts)} 个账号 ---")
            results.append(process_account(account, playwright, headless=not args.headed))
    logger.info("================ 结果汇总 ================")
    ok = 0
    for result in results:
        logger.info(f"[{ 'OK' if result['ok'] else 'FAIL' }] {result['email']} | status={result['status']} | {result['error']}")
        if result["ok"]:
            ok += 1
    logger.info(f"成功 {ok}/{len(results)}")
    if notify.tg_enabled():
        notify.notify_summary(results)
    sys.exit(0 if ok == len(results) and ok > 0 else 1)


if __name__ == "__main__":
    main()
