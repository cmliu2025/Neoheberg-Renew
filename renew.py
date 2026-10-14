#!/usr/bin/env python3
"""
NeoHeberg 自动续期 + 开机守护（GitHub Actions）

流程：无头浏览器登录（两步式表单 + Cap.js）→ 读取到期日 → 剩余天数 <= RENEW_DAYS 则续期
      → 容器若已停止则开机。

环境变量：
  NEO_USER      面板账号（必填）
  NEO_PASSWORD  面板密码（必填）
  NEO_VMID      服务实例 ID（必填，面板服务列表里可见）
  NEO_TYPE      服务类型，默认 vps
  RENEW_DAYS    剩余多少天时续期，默认 14
  PROXY_SERVER  可选，如 socks5://127.0.0.1:1080（被 Cloudflare 拦截时用）
"""
import json
import os
import re
import sys
import time
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://dash.neoheberg.fr"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

USER = os.environ.get("NEO_USER", "")
PASSWORD = os.environ.get("NEO_PASSWORD", "")
VMID = os.environ.get("NEO_VMID", "")
TYPE = os.environ.get("NEO_TYPE", "vps")
RENEW_DAYS = int(os.environ.get("RENEW_DAYS", "14"))
PROXY = os.environ.get("PROXY_SERVER", "")
ATTEMPTS = int(os.environ.get("LOGIN_ATTEMPTS", "3"))
BACKOFF = [15, 60, 180]  # 登录失败后的等待秒数（站点有 429 速率限制）


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def mask(s, show=2):
    s = str(s)
    return s if len(s) <= show * 2 else s[:show] + "*" * (len(s) - show * 2) + s[-show:]


def snap(page, name):
    """失败时保存截图 + HTML，供 Actions artifacts 下载排查。"""
    try:
        page.screenshot(path=f"{name}.png", full_page=True)
        with open(f"{name}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass


def read_csrf(page):
    try:
        return page.evaluate("""() => {
            const i = document.querySelector('input[name="csrf_token"]');
            if (i && i.value) return i.value;
            const m = document.querySelector('meta[name="csrf-token"]');
            if (m && m.content) return m.content;
            if (typeof window.CSRF === 'string') return window.CSRF;
            return '';
        }""") or ""
    except Exception:
        return ""


# ───────────────────────── 登录 ─────────────────────────
def login_once(page, ctx):
    page.goto(BASE + "/login", wait_until="domcontentloaded", timeout=60000)

    # 第一步：账号
    try:
        page.wait_for_selector('input[name="identifier"]', timeout=45000)
    except PWTimeout:
        log("登录表单未渲染（站点可能返回了残缺页面，或被 Cloudflare 拦截）")
        snap(page, "login_no_form")
        return False
    page.fill('input[name="identifier"]', USER)
    page.click("#goToPassword")

    # 第二步：密码框出现
    try:
        page.wait_for_selector('input[name="password"]', timeout=25000)
    except PWTimeout:
        log("密码框未出现")
        snap(page, "login_no_password")
        return False

    # Cap.js 验证码：触发求解，轮询 cap-token（约 10~20 秒）
    page.evaluate("() => { const w = document.getElementById('cap-login'); if (w && w.solve) w.solve(); }")
    token = ""
    for _ in range(30):
        page.wait_for_timeout(2000)
        token = page.evaluate(
            "() => { const e = document.querySelector('input[name=\"cap-token\"]');"
            " return e ? e.value : ''; }")
        if token and len(token) > 20:
            break
    if not token:
        log("验证码求解超时")
        snap(page, "login_cap_timeout")
        return False
    log("验证码通过")

    page.fill('input[name="password"]', PASSWORD)
    csrf = read_csrf(page)
    page.evaluate("""(csrf) => {
        const f = document.querySelector('input[name="password"]').form;
        if (csrf && !f.querySelector('input[name="csrf_token"]')) {
            const h = document.createElement('input');
            h.type = 'hidden'; h.name = 'csrf_token'; h.value = csrf;
            f.appendChild(h);
        }
        f.submit();
    }""", csrf)

    try:
        page.wait_for_url(re.compile(r"dash\.neoheberg\.fr/(\?.*)?$"), timeout=45000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2000)

    names = {c["name"] for c in ctx.cookies()}
    if "__Host-NH-Remember" in names or "/login" not in page.url:
        return True
    log(f"登录未成功，当前 URL: {page.url}")
    snap(page, "login_failed")
    return False


def login(page, ctx):
    for i in range(1, ATTEMPTS + 1):
        if i > 1:
            wait = BACKOFF[min(i - 2, len(BACKOFF) - 1)]
            log(f"第 {i}/{ATTEMPTS} 次登录尝试，等待 {wait}s …")
            time.sleep(wait)
        else:
            log(f"第 {i}/{ATTEMPTS} 次登录尝试 …")
        try:
            if login_once(page, ctx):
                return True
        except Exception as e:
            log(f"登录异常: {type(e).__name__}: {str(e)[:150]}")
            snap(page, "login_error")
    return False


# ───────────────────────── 面板操作（复用浏览器 cookie 走 HTTP） ─────────────────────────
def get_text(ctx, path):
    return ctx.request.get(BASE + path, headers={"Accept-Language": "fr-FR"}).text()


def get_json(ctx, path):
    r = ctx.request.get(BASE + path, headers={
        "Accept": "application/json", "X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/"})
    return json.loads(r.text())


def post(ctx, path, form, ajax=False):
    headers = {"Origin": BASE, "Referer": BASE + "/"}
    if ajax:
        headers.update({"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
    return ctx.request.post(BASE + path, form=form, headers=headers, timeout=120000)


def panel_info(ctx):
    html = get_text(ctx, "/")
    m = re.search(r'CSRF\s*=\s*["\']([^"\']+)', html) or \
        re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    e = re.search(r'Échéance le\s*([0-9]{2}/[0-9]{2}/[0-9]{4})', html)
    d = re.search(r'(\d+)\s*jours?\s*restants?', html)
    return (m.group(1) if m else None,
            e.group(1) if e else None,
            int(d.group(1)) if d else None)


def do_renew(ctx, csrf):
    r = post(ctx, "/services/renew", {"csrf_token": csrf, "type": TYPE, "id": VMID})
    body = r.text()
    m = re.search(r'"message"\s*:\s*"([^"]*)"', body)
    msg = ""
    if m:
        try:
            msg = json.loads('"' + m.group(1) + '"')
        except Exception:
            msg = m.group(1)
    return ("renouvel" in msg.lower()), (msg or f"HTTP {r.status}")


def keep_running(ctx, csrf):
    """容器已停止则开机；失败不影响续期结果。"""
    try:
        st = get_json(ctx, f"/app/services/vps-stats.php?id={VMID}")
        status = (st.get("status") or "unknown").lower()
        log(f"容器状态: {status}")
        if status in ("stopped", "halted", "shutdown"):
            r = post(ctx, "/app/services/vps-power.php",
                     {"vmid": VMID, "signal": "start", "csrf_token": csrf}, ajax=True)
            log(f"已下发开机指令: HTTP {r.status}")
    except Exception as e:
        log(f"状态/开机检查跳过: {type(e).__name__}: {str(e)[:100]}")


# ───────────────────────── 主流程 ─────────────────────────
def main():
    missing = [k for k, v in (("NEO_USER", USER), ("NEO_PASSWORD", PASSWORD), ("NEO_VMID", VMID)) if not v]
    if missing:
        log(f"缺少环境变量: {', '.join(missing)}")
        return 1
    log(f"账号={mask(USER)} 实例={mask(VMID)} 续期阈值={RENEW_DAYS}天")

    with sync_playwright() as p:
        kw = dict(headless=True, args=["--disable-blink-features=AutomationControlled",
                                       "--no-sandbox", "--disable-dev-shm-usage"])
        if PROXY:
            kw["proxy"] = {"server": PROXY}
        browser = p.chromium.launch(**kw)
        ctx = browser.new_context(user_agent=UA, locale="fr-FR",
                                  viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()
        try:
            if not login(page, ctx):
                log("❌ 登录失败")
                return 1
            log("✅ 登录成功")

            csrf, exp, days = panel_info(ctx)
            log(f"到期日: {exp}  剩余: {days} 天")
            if not csrf:
                log("❌ 未取到 CSRF token")
                snap(page, "no_csrf")
                return 1

            rc = 0
            if days is None or days <= RENEW_DAYS:
                ok, msg = do_renew(ctx, csrf)
                log(f"续期{'成功' if ok else '失败'}: {msg}")
                rc = 0 if ok else 1
                _, exp2, days2 = panel_info(ctx)
                log(f"续期后到期日: {exp2}  剩余: {days2} 天")
            else:
                log(f"剩余天数 > {RENEW_DAYS}，本次无需续期")

            keep_running(ctx, csrf)
            return rc
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
