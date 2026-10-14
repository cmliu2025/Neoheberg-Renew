import os, re, sys
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://dash.neoheberg.fr"
LOGIN_URL = BASE + "/login"
EMAIL = os.environ["NEO_EMAIL"]
PASSWORD = os.environ["NEO_PASSWORD"]

# 续期按钮文字（中/英/法都兼容），如不匹配请按实际页面修改
RENEW_RE = re.compile(r"renew|renouvel|prolong|extend|续期", re.I)


def shot(page, name):
    try:
        page.screenshot(path=name, full_page=True)
    except Exception:
        pass


def dump_html(page, name):
    try:
        with open(name, "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass


def info(page, tag):
    """打印诊断信息并保存截图 + HTML，方便排查。"""
    print(f"[{tag}] URL:", page.url)
    try:
        print(f"[{tag}] 标题:", page.title())
        print(f"[{tag}] 文字:", page.inner_text("body")[:300].replace("\n", " | "))
    except Exception:
        pass
    shot(page, f"{tag}.png")
    dump_html(page, f"{tag}.html")


def solve_cap(page, timeout=60000):
    """点击 Cap 验证码（工作量证明），等待它在浏览器里算完。"""
    widget = page.locator("cap-widget")
    label = page.get_by_text(re.compile(r"v[ée]rifier que vous|verify you|i'?m a human", re.I))
    try:
        widget.or_(label).first.wait_for(state="visible", timeout=8000)
    except PWTimeout:
        print("未发现验证码，跳过")
        return
    inner = page.locator("cap-widget .checkbox, cap-widget [role=checkbox]")
    target = inner.first if inner.count() else (label.first if label.count() else widget.first)
    target.click()
    print("已点击验证码，等待计算…")
    try:
        page.wait_for_function(
            """() => { const i = document.querySelector('input[name="cap-token"]');
                       return i && i.value && i.value.length > 0; }""",
            timeout=timeout,
        )
        print("验证码通过")
    except PWTimeout:
        print("未检测到 cap-token，兜底等待 8 秒")
        page.wait_for_timeout(8000)


def run(page):
    # 1. 打开登录页
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeout:
        pass
    page.wait_for_timeout(3000)
    info(page, "login_page")

    # 2. 填账号密码（账号框：第一个可见的非密码/隐藏/复选框 input）
    user_input = page.locator(
        "input:not([type=password]):not([type=hidden]):not([type=checkbox])"
        ":not([type=submit]):not([type=button])"
    ).first
    user_input.fill(EMAIL, timeout=15000)
    page.locator('input[type="password"]').first.fill(PASSWORD, timeout=15000)
    solve_cap(page)
    page.locator('button[type="submit"], input[type="submit"]').first.click()

    try:
        page.wait_for_url(lambda u: "/login" not in u, timeout=30000)
    except PWTimeout:
        info(page, "login_failed")
        print("登录失败：账号密码错误或验证码未通过", file=sys.stderr)
        sys.exit(1)
    print("登录成功:", page.url)
    page.wait_for_load_state("networkidle")
    info(page, "after_login")

    # 3. 找续期按钮（当前页没有就尝试常见页面）
    def find_btn():
        return page.get_by_role("button", name=RENEW_RE).or_(
            page.get_by_role("link", name=RENEW_RE)
        )

    btn = find_btn()
    if btn.count() == 0:
        for path in ("/services", "/service", "/hosting", "/dashboard"):
            page.goto(BASE + path, wait_until="networkidle")
            btn = find_btn()
            if btn.count():
                break

    if btn.count() == 0:
        info(page, "no_renew_button")
        print("未找到续期按钮（可能还没到可续期时间，或需要调整选择器）")
        return

    # 4. 点击续期
    for i in range(btn.count()):
        b = btn.nth(i)
        print("点击:", b.inner_text().strip())
        b.click()
        solve_cap(page, timeout=30000)
        confirm = page.get_by_role("button", name=re.compile(r"confirm|oui|yes|ok|valider", re.I))
        if confirm.count():
            confirm.first.click()
        page.wait_for_load_state("networkidle")

    info(page, "renewed")
    print("续期完成")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            locale="fr-FR",
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        page.set_default_timeout(30000)
        try:
            run(page)
        except SystemExit:
            raise
        except Exception:
            info(page, "error")  # 任何异常都保存现场
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
