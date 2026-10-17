#!/usr/bin/env python3
"""
NeoHeberg 自动续期 + 开机守护（GitHub Actions，支持多账号）

流程（每个账号独立）：无头浏览器登录（两步式表单 + Cap.js）→ 读取到期日
      → 剩余天数 <= RENEW_DAYS 则续期 → 容器若已停止则开机。

账号配置（二选一）：
  ① 多账号：Secret NEO_ACCOUNTS
     JSON（推荐，密码里有任何特殊字符都安全）：
       [
         {"user": "账号1", "password": "密码1", "vmid": "12345"},
         {"user": "账号2", "password": "密码2", "vmid": "67890", "proxy": "http://u:p@1.2.3.4:8080"}
       ]
     可选字段: name(日志里的别名) / type(默认 vps) / renew_days / proxy(该账号专用代理，缺省用全局代理)
     或每行一个（密码可含 | 以外的任意字符，# 开头为注释）：
       账号1|密码1|12345
       账号2|密码2|67890
  ② 单账号（兼容旧配置）：NEO_USER / NEO_PASSWORD / NEO_VMID
  两种可同时存在：旧的单账号变量会作为第一个账号，与 NEO_ACCOUNTS 合并处理（相同账号+实例自动去重）。

其他环境变量：
  NEO_TYPE      服务类型，默认 vps
  RENEW_DAYS    剩余多少天时续期，默认 14
  PROXY_SERVER  全局代理，如 socks5://127.0.0.1:1080 或 http://user:pass@host:port
                （workflow 里配置 NODE_LINK 时由 setup_proxy.py 自动导出）
  FORCE_RENEW   设为 true 则忽略剩余天数，强制续期
  ACCOUNT_GAP   账号之间的间隔秒数（另加 0~15 秒随机），默认 30

代理降级规则（每个账号独立判断，不可用的代理绝不会悄悄改成直连）：
  · 有专用代理 → 先用专用代理；连不上或出口被 Cloudflare 拦截，则自动改用全局代理
  · 没有专用代理 → 用全局代理；全局代理失效则该账号被取消
  · 全局代理与专用代理都失效/被拦截 → 该账号被取消；所有账号都被取消则整体失败退出
  · 某个出口被 Cloudflare 拦截后，本次运行内不再让其他账号去撞这个出口
  · 一个代理都没配置 → 直连

Telegram 推送（可选）：
  TG_BOT_TOKEN  机器人 Token（@BotFather 创建）
  TG_CHAT_ID    接收消息的 Chat ID
  TG_NOTIFY     always(默认,每次都发) / change(有续期或失败或代理降级才发) / fail(仅失败才发)
"""
import html
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse, unquote
from urllib.request import Request, urlopen
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://dash.neoheberg.fr"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

DEFAULT_TYPE = (os.environ.get("NEO_TYPE") or "vps").strip()
RENEW_DAYS = int(os.environ.get("RENEW_DAYS") or 14)
PROXY = os.environ.get("PROXY_SERVER", "").strip()
FORCE_RENEW = os.environ.get("FORCE_RENEW", "").lower() in ("1", "true", "yes")
# setup_proxy.py 在 NODE_LINK 启动失败时写入；表示"配置了全局代理但它不可用"
GLOBAL_PROXY_FAILED = os.environ.get("GLOBAL_PROXY_FAILED", "").lower() in ("1", "true", "yes")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TG_CHAT_ID", "").strip()
TG_NOTIFY = (os.environ.get("TG_NOTIFY", "") or "always").strip().lower()  # always / change / fail
ATTEMPTS = int(os.environ.get("LOGIN_ATTEMPTS") or 3)
ACCOUNT_GAP = int(os.environ.get("ACCOUNT_GAP") or 30)
BACKOFF = [15, 60, 180]  # 登录失败后的等待秒数（站点有 429 速率限制）

LABEL = ""      # 当前账号标签，用于日志前缀
FILE_TAG = ""   # 当前账号的调试文件前缀（多账号时区分截图）


def log(msg):
    prefix = f"[{LABEL}] " if LABEL else ""
    print(f"[{time.strftime('%H:%M:%S')}] {prefix}{msg}", flush=True)


def mask(s, show=2):
    s = str(s)
    return s if len(s) <= show * 2 else s[:show] + "*" * (len(s) - show * 2) + s[-show:]


# ───────────────────────── 账号配置 ─────────────────────────
def _norm(entry, idx):
    """把一条账号配置规范化；返回 (账号dict, 错误信息)。"""
    g = lambda *keys: next((str(entry[k]).strip() for k in keys if entry.get(k) not in (None, "")), "")
    acc = {
        "user": g("user", "username", "email", "identifier"),
        "password": g("password", "pass", "pwd"),
        "vmid": g("vmid", "id"),
        "type": g("type") or DEFAULT_TYPE,
        "proxy": g("proxy"),
        "name": g("name", "label") or f"#{idx}",
    }
    rd = g("renew_days")
    acc["renew_days"] = int(rd) if rd.isdigit() else RENEW_DAYS
    missing = [k for k in ("user", "password", "vmid") if not acc[k]]
    return acc, (f"第 {idx} 个账号缺少字段: {', '.join(missing)}" if missing else "")


def parse_accounts():
    """返回 (账号列表, 错误列表)。
    来源：旧的单账号变量 NEO_USER/NEO_PASSWORD/NEO_VMID（若设置，作为第一个账号）
          + NEO_ACCOUNTS（JSON 或每行一个）。相同的 账号+实例 自动去重。"""
    entries = []
    if os.environ.get("NEO_USER") or os.environ.get("NEO_PASSWORD") or os.environ.get("NEO_VMID"):
        entries.append({"user": os.environ.get("NEO_USER", ""), "password": os.environ.get("NEO_PASSWORD", ""),
                        "vmid": os.environ.get("NEO_VMID", "")})

    raw = os.environ.get("NEO_ACCOUNTS", "").strip()
    if raw:
        if raw.startswith("["):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                return [], [f"NEO_ACCOUNTS 不是合法 JSON（第 {e.lineno} 行第 {e.colno} 列附近）"]
            if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
                return [], ["NEO_ACCOUNTS 应为对象数组 [{...}, {...}]"]
            entries += data
        else:
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) < 3:
                    entries.append({})  # 占位，让后面报"缺少字段"
                    continue
                # 账号取第一段、vmid 取最后一段，中间都算密码（密码里可以有 |）
                entries.append({"user": parts[0], "password": "|".join(parts[1:-1]), "vmid": parts[-1]})

    accounts, errors, seen = [], [], set()
    for i, e in enumerate(entries, 1):
        acc, err = _norm(e, i)
        if err:
            errors.append(err)
            continue
        key = (acc["user"], acc["vmid"])
        if key in seen:
            errors.append(f"第 {i} 个账号与前面重复（相同账号+实例），已忽略")
            continue
        seen.add(key)
        accounts.append(acc)
    if not entries:
        errors.append("未配置账号：请设置 NEO_ACCOUNTS，或 NEO_USER / NEO_PASSWORD / NEO_VMID")
    return accounts, errors


def build_proxy(url):
    """代理 URL -> Playwright proxy 参数。支持 http/https/socks5，可带账号密码。"""
    if not url:
        return None
    u = urlparse(url)
    scheme = {"socks5h": "socks5"}.get(u.scheme, u.scheme)
    if scheme not in ("http", "https", "socks5", "socks4") or not u.hostname or not u.port:
        raise ValueError("代理格式应为 scheme://[user:pass@]host:port（http/https/socks5）")
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    proxy = {"server": f"{scheme}://{host}:{u.port}"}
    if u.username:
        if scheme.startswith("socks"):
            log("⚠️ Chromium 不支持带账号密码的 SOCKS5，请改用 NODE_LINK（sing-box）或 HTTP 代理")
        proxy["username"] = unquote(u.username)
        proxy["password"] = unquote(u.password or "")
    return proxy


def snap(page, name):
    """失败时保存截图 + HTML，供 Actions artifacts 下载排查。"""
    try:
        page.screenshot(path=f"{FILE_TAG}{name}.png", full_page=True)
        with open(f"{FILE_TAG}{name}.html", "w", encoding="utf-8") as f:
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
BLOCKED = "blocked"      # 被 Cloudflare 托管质询拦截
BADCREDS = "badcreds"    # 站点提示账号或密码无效
FAILED = "failed"        # 其他失败


def cf_blocked(page):
    """当前页面是否是 Cloudflare 质询页（注意：正常页面也会带 challenge-platform 脚本，不能只看它）。"""
    try:
        title = page.title()
        html = page.content()
    except Exception:
        return False
    return title.startswith(("Un instant", "Just a moment", "Attention Required")) or "_cf_chl_opt" in html


def wait_cf_clear(page, timeout=25):
    """遇到质询页先等一会儿（非交互式质询会自动通过）；仍未通过则返回 False。"""
    if not cf_blocked(page):
        return True
    log("检测到 Cloudflare 质询页，等待自动通过 …")
    end = time.time() + timeout
    while time.time() < end:
        page.wait_for_timeout(2000)
        if not cf_blocked(page):
            log("质询已通过")
            return True
    snap(page, "cloudflare_challenge")
    return False


def login_once(page, ctx, user, password):
    """返回 True / BLOCKED / BADCREDS / FAILED。"""
    page.goto(BASE + "/login", wait_until="domcontentloaded", timeout=60000)
    if not wait_cf_clear(page):
        return BLOCKED

    # 第一步：账号
    try:
        page.wait_for_selector('input[name="identifier"]', timeout=45000)
    except PWTimeout:
        if cf_blocked(page):
            snap(page, "cloudflare_challenge")
            return BLOCKED
        log("登录表单未渲染（站点可能返回了残缺页面）")
        snap(page, "login_no_form")
        return FAILED
    page.fill('input[name="identifier"]', user)
    page.click("#goToPassword")

    # 第二步：密码框出现
    try:
        page.wait_for_selector('input[name="password"]', timeout=25000)
    except PWTimeout:
        log("密码框未出现")
        snap(page, "login_no_password")
        return FAILED

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
        return FAILED
    log("验证码通过")

    page.fill('input[name="password"]', password)
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
    if cf_blocked(page):
        log("提交登录后被 Cloudflare 质询拦截")
        snap(page, "cloudflare_challenge")
        return BLOCKED
    try:
        err = page.evaluate("""() => { const a = document.querySelector('[role=alert], .alert-card');
                                       return a ? a.innerText.trim() : ''; }""")
    except Exception:
        err = ""
    err = re.sub(r"\s+", " ", err or "")
    if err:
        log(f"站点提示: {err}")
    snap(page, "login_failed")
    if re.search(r"invalide|invalid|incorrect", err, re.I):
        return BADCREDS
    log(f"登录未成功，当前 URL: {page.url}")
    return FAILED


def login(page, ctx, user, password):
    """返回 True / BLOCKED / BADCREDS / FAILED。账号密码错误或被质询时不重试。"""
    for i in range(1, ATTEMPTS + 1):
        if i > 1:
            wait = BACKOFF[min(i - 2, len(BACKOFF) - 1)]
            log(f"第 {i}/{ATTEMPTS} 次登录尝试，等待 {wait}s …")
            time.sleep(wait)
        else:
            log(f"第 {i}/{ATTEMPTS} 次登录尝试 …")
        try:
            res = login_once(page, ctx, user, password)
        except Exception as e:
            log(f"登录异常: {type(e).__name__}: {str(e)[:150]}")
            snap(page, "login_error")
            continue
        if res is True:
            return True
        if res == BADCREDS:
            log("❌ 账号或密码被站点拒绝，已停止重试（继续重试可能触发限流或锁号）。")
            log("   请检查该账号的用户名/密码，并确认账号设置过密码（Discord 登录的账号默认没有密码）。")
            return BADCREDS
        if res == BLOCKED:
            log("❌ 被 Cloudflare 托管质询（Turnstile）拦截：当前出口 IP 信誉低，重试只会更糟，已停止。")
            log("   解决办法：配置 Secret NODE_LINK 或 PROXY_SERVER，使用干净的固定出口 IP"
                "（住宅/家宽 IP 最稳），或改用自托管 runner。")
            return BLOCKED
    return FAILED


# ───────────────────────── 面板操作（在已登录页面内 fetch，自动走同一代理与 cookie） ─────────────────────────
def ensure_origin(page):
    if not page.url.startswith(BASE):
        page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)


def panel_fetch(page, path, form=None, ajax=False):
    ensure_origin(page)
    return page.evaluate("""async ([path, form, ajax]) => {
        const headers = {};
        if (ajax) { headers['Accept'] = 'application/json'; headers['X-Requested-With'] = 'XMLHttpRequest'; }
        const opt = { method: form ? 'POST' : 'GET', credentials: 'same-origin', headers };
        if (form) opt.body = new URLSearchParams(form);
        const r = await fetch(path, opt);
        return { status: r.status, text: await r.text() };
    }""", [path, form, ajax])


def panel_info(page):
    html = panel_fetch(page, "/")["text"]
    m = re.search(r'CSRF\s*=\s*["\']([^"\']+)', html) or \
        re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    e = re.search(r'Échéance le\s*([0-9]{2}/[0-9]{2}/[0-9]{4})', html)
    d = re.search(r'(\d+)\s*jours?\s*restants?', html)
    return (m.group(1) if m else None,
            e.group(1) if e else None,
            int(d.group(1)) if d else None)


def do_renew(page, csrf, acc):
    r = panel_fetch(page, "/services/renew", {"csrf_token": csrf, "type": acc["type"], "id": acc["vmid"]})
    body = r["text"]
    m = re.search(r'"message"\s*:\s*"([^"]*)"', body)
    msg = ""
    if m:
        try:
            msg = json.loads('"' + m.group(1) + '"')
        except Exception:
            msg = m.group(1)
    return ("renouvel" in msg.lower()), (msg or f"HTTP {r['status']}")


def keep_running(page, csrf, acc):
    """容器已停止则开机；失败不影响续期结果。"""
    try:
        st = json.loads(panel_fetch(page, f"/app/services/vps-stats.php?id={acc['vmid']}", ajax=True)["text"])
        status = (st.get("status") or "unknown").lower()
        log(f"容器状态: {status}")
        if status in ("stopped", "halted", "shutdown"):
            r = panel_fetch(page, "/app/services/vps-power.php",
                            {"vmid": acc["vmid"], "signal": "start", "csrf_token": csrf}, ajax=True)
            log(f"已下发开机指令: HTTP {r['status']}")
    except Exception as e:
        log(f"状态/开机检查跳过: {type(e).__name__}: {str(e)[:100]}")


# ───────────────────────── 代理选择 ─────────────────────────
PROXY_CACHE = {}  # 代理 URL -> 出口 IP / None，同一个代理只检测一次


def check_proxy(p, url):
    """经该代理访问 ipify。成功返回出口 IP，失败返回 None（结果按 URL 缓存）。"""
    if url in PROXY_CACHE:
        return PROXY_CACHE[url]
    try:
        pr = build_proxy(url)
    except ValueError as e:
        log(f"⚠️ {e}")
        PROXY_CACHE[url] = None
        return None
    ip = None
    for attempt in (1, 2):  # 偶发抖动给一次重试机会
        browser = None
        try:
            browser = p.chromium.launch(headless=True, proxy=pr,
                                        args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page()
            page.goto("https://api.ipify.org", timeout=20000)
            text = page.inner_text("body").strip()
            ip = text if re.fullmatch(r"[0-9a-fA-F:.]{7,45}", text) else None
        except Exception:
            ip = None
        finally:
            if browser:
                browser.close()
        if ip:
            break
        if attempt == 1:
            time.sleep(3)
    PROXY_CACHE[url] = ip
    return ip


def iter_choices(p, acc, blocked_keys, info):
    """按优先级依次产出该账号可用的出口：专用代理 → 全局代理。

    调用方用完一个出口后若发现它被 Cloudflare 拦截，把它加入 blocked_keys 再取下一个即可。
    规则：
      · 有专用代理 → 先用专用代理；不可用（连不上）或被 Cloudflare 拦截 → 自动改用全局代理
      · 没有专用代理 → 用全局代理；全局代理不可用 → 取消该账号（不直连）
      · 全局代理（含 NODE_LINK 启动失败）与专用代理都不可用/被拦截 → 取消该账号
      · 完全没有配置任何代理 → 直连
    取消原因写入 info["note"]。
    """
    cands = []
    if acc["proxy"]:
        cands.append(("专用代理", acc["proxy"]))
    if PROXY and PROXY != acc["proxy"]:
        cands.append(("全局代理", PROXY))

    if not cands:
        if GLOBAL_PROXY_FAILED:
            log("❌ 全局代理不可用，且该账号未配置专用代理，已取消（不会直连）")
            info["note"] = "全局代理失效且无专用代理，已取消"
        elif "(direct)" in blocked_keys:
            log("⏭ 跳过：直连出口已被 Cloudflare 拦截，继续尝试只会更糟")
            info["note"] = "跳过（出口 IP 已被拦截）"
        else:
            yield {"url": None, "via": "直连", "key": "(direct)"}
        return

    if GLOBAL_PROXY_FAILED and not PROXY:
        log("⚠️ 全局代理（NODE_LINK）启动失败，仅尝试该账号的专用代理")

    reason, skipped_blocked = "", False
    for name, url in cands:
        if url in blocked_keys:
            log(f"⏭ {name}出口已被 Cloudflare 拦截，跳过")
            skipped_blocked, reason = True, "被拦截"
            continue
        ip = check_proxy(p, url)
        if not ip:
            log(f"⚠️ {name}不可用")
            reason = "失效"
            continue
        via = name
        if reason:
            via = f"{name}（专用代理{reason}）"
            log(f"⚠️ 专用代理{reason}，已自动改用{name}")
        log(f"{name}可用，出口 IP: {mask(ip, 3)}")
        yield {"url": url, "via": via, "key": url}
        reason = "被拦截"  # 调用方回来继续取下一个，说明刚才这个出口被 Cloudflare 拦截了

    info["note"] = "跳过（出口 IP 已被拦截）" if skipped_blocked else "代理全部失效，已取消"


# ───────────────────────── Telegram 推送 ─────────────────────────
def tg_send(text):
    """发送 Telegram 消息。任何失败都只记录日志，绝不影响续期结果（也不会把 token 打到日志里）。"""
    if not (TG_TOKEN and TG_CHAT):
        return False
    try:
        data = urlencode({"chat_id": TG_CHAT, "text": text[:3900], "parse_mode": "HTML",
                          "disable_web_page_preview": "true"}).encode()
        req = Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data)
        with urlopen(req, timeout=20) as r:
            ok = r.status == 200
        log("📨 Telegram 推送成功" if ok else "⚠️ Telegram 推送失败")
        return ok
    except HTTPError as e:
        hint = {401: "（Bot Token 无效）", 400: "（Chat ID 无效，或还没给机器人发过消息）",
                403: "（机器人被拉黑或无权限）"}.get(e.code, "")
        log(f"⚠️ Telegram 推送失败: HTTP {e.code}{hint}")
    except Exception as e:
        log(f"⚠️ Telegram 推送失败: {type(e).__name__}")
    return False


def should_notify(results, errors):
    if errors and not results:
        return True
    failed = any(s == "failed" for _, s, _, _ in results)
    changed = any(s == "renewed" or "（" in v for _, s, _, v in results)
    return {"always": True, "fail": failed, "change": failed or changed}.get(TG_NOTIFY, True)


def build_report(results, errors):
    bj = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    icon = {"renewed": "✅", "ok": "✔️", "failed": "❌"}
    failed = sum(1 for _, s, _, _ in results if s == "failed")
    lines = [f"<b>NeoHeberg 续期报告</b>", f"🕐 {bj}（北京时间）", ""]
    for name, status, note, via in results:
        tail = f" · <i>{html.escape(via)}</i>" if "（" in via else ""
        lines.append(f"{icon[status]} <b>{html.escape(name)}</b>：{html.escape(note)}{tail}")
    for e in errors:
        lines.append(f"⚠️ 配置提示：{html.escape(e)}")
    lines += ["", f"成功 {len(results) - failed} / 失败 {failed} / 共 {len(results)}"]
    repo, run = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID")
    if repo and run:
        lines.append(f'<a href="{os.environ.get("GITHUB_SERVER_URL", "https://github.com")}/{repo}/actions/runs/{run}">查看运行日志</a>')
    return "\n".join(lines)


# ───────────────────────── 单个账号 ─────────────────────────
def process_account(p, acc, choice):
    """处理一个账号（出口已由 iter_choices 选好）。返回 dict: status(renewed/ok/failed), note, blocked, via。"""
    via = choice["via"]
    result = lambda status, note, blocked=False: {"status": status, "note": note, "blocked": blocked, "via": via}

    proxy_url = choice["url"]
    kw = dict(headless=True, args=["--disable-blink-features=AutomationControlled",
                                   "--no-sandbox", "--disable-dev-shm-usage"])
    if proxy_url:
        try:
            kw["proxy"] = build_proxy(proxy_url)
        except ValueError as e:
            log(f"❌ {e}")
            return result("failed", "代理配置错误")
        log(f"使用{via}: {kw['proxy']['server'].split('://')[0]}://{mask(urlparse(proxy_url).hostname, 3)}")

    browser = p.chromium.launch(**kw)
    try:
        ctx = browser.new_context(user_agent=UA, locale="fr-FR", viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()
        page.set_default_timeout(30000)
        try:
            res = login(page, ctx, acc["user"], acc["password"])
            if res is not True:
                note = {BLOCKED: "被 Cloudflare 拦截", BADCREDS: "账号或密码错误"}.get(res, "登录失败")
                return result("failed", note, res == BLOCKED)
            log("✅ 登录成功")

            csrf, exp, days = panel_info(page)
            log(f"到期日: {exp}  剩余: {days} 天")
            if not csrf:
                log("❌ 未取到 CSRF token")
                snap(page, "no_csrf")
                return result("failed", "未取到 CSRF token")

            threshold = acc["renew_days"]
            if FORCE_RENEW or days is None or days <= threshold:
                if FORCE_RENEW:
                    log("已指定强制续期")
                ok, msg = do_renew(page, csrf, acc)
                log(f"续期{'成功' if ok else '失败'}: {msg}")
                _, exp2, days2 = panel_info(page)
                log(f"续期后到期日: {exp2}  剩余: {days2} 天")
                keep_running(page, csrf, acc)
                if ok:
                    return result("renewed", f"续期成功（剩余 {days} → {days2} 天）")
                return result("failed", f"续期失败: {msg}")

            log(f"剩余天数 > {threshold}，本次无需续期")
            keep_running(page, csrf, acc)
            return result("ok", f"无需续期（剩余 {days} 天）")
        except Exception as e:
            log(f"❌ 处理异常: {type(e).__name__}: {str(e)[:150]}")
            snap(page, "error")
            return result("failed", f"异常: {type(e).__name__}")
    finally:
        browser.close()


# ───────────────────────── 主流程 ─────────────────────────
def main():
    global LABEL, FILE_TAG
    for secret in (TG_TOKEN, TG_CHAT):  # 让 Actions 日志屏蔽 TG 凭据
        if secret:
            print(f"::add-mask::{secret}", flush=True)

    accounts, errors = parse_accounts()
    for e in errors:
        log(f"⚠️ 配置问题: {e}")
    if not accounts:
        log("❌ 没有可用的账号配置")
        tg_send("❌ <b>NeoHeberg 续期</b>：没有可用的账号配置\n" + "\n".join(html.escape(e) for e in errors))
        return 1

    # 让 Actions 日志屏蔽账号和密码（防止公开仓库泄露）
    for a in accounts:
        for v in (a["password"], a["user"]):
            if len(v) >= 4 and "\n" not in v:
                print(f"::add-mask::{v}", flush=True)

    total = len(accounts)
    log(f"共 {total} 个账号，续期阈值默认 {RENEW_DAYS} 天" + ("，强制续期" if FORCE_RENEW else ""))
    results, blocked_keys, ran = [], set(), False

    with sync_playwright() as p:
        for i, acc in enumerate(accounts, 1):
            LABEL = acc["name"] if total > 1 else ""
            FILE_TAG = f"acc{i}_" if total > 1 else ""

            info, final, last_blocked, tried = {}, None, None, 0
            for choice in iter_choices(p, acc, blocked_keys, info):
                if tried:
                    time.sleep(5)  # 同一账号换出口重试前稍等
                elif ran:  # 账号之间留间隔，避免触发站点限流
                    gap = ACCOUNT_GAP + random.uniform(0, 15)
                    log(f"等待 {gap:.0f}s 后处理下一个账号 …")
                    time.sleep(gap)
                ran, tried = True, tried + 1

                if tried == 1:
                    log(f"开始处理（{i}/{total}）：账号={mask(acc['user'])}（{len(acc['user'])}位） "
                        f"密码={len(acc['password'])}位 实例={mask(acc['vmid'])}")
                res = process_account(p, acc, choice)
                if res["blocked"]:  # 被 Cloudflare 拦截：记下这个出口，尝试下一个
                    blocked_keys.add(choice["key"])
                    last_blocked = res
                    continue
                final = res
                break

            if final is None:
                if last_blocked:
                    final = dict(last_blocked, note="被 Cloudflare 拦截" + ("（所有出口均被拦截）" if tried > 1 else ""))
                else:
                    final = {"status": "failed", "note": info.get("note", "代理全部失效，已取消"), "via": ""}
            results.append((acc["name"], final["status"], final["note"], final["via"]))

    LABEL = ""
    log("═════════ 汇总 ═════════")
    icon = {"renewed": "✅", "ok": "✔️", "failed": "❌"}
    for name, status, note, via in results:
        log(f"{icon[status]} {name}: {note}" + (f"  [{via}]" if via else ""))
    failed = sum(1 for _, s, _, _ in results if s == "failed")
    log(f"成功 {len(results) - failed} / 失败 {failed} / 共 {len(results)}")

    if should_notify(results, errors):
        tg_send(build_report(results, errors))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
