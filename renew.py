import os, re, sys
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

LOGIN_URL = "https://dash.neoheberg.fr/login"
EMAIL = os.environ["NEO_EMAIL"]
PASSWORD = os.environ["NEO_PASSWORD"]

# 续期按钮文字（中/英/法都兼容），如不匹配请按实际页面修改
RENEW_RE = re.compile(r"renew|renouvel|prolong|extend|续期", re.I)


def shot(page, name):
    try:
        page.screenshot(path=name, full_page=True)
    except Exception:
        pass


def solve_cap(page, timeout=60000):
    """点击 Cap 验证码（工作量证明），等待它在浏览器里算完。"""
    widget = page.locator("cap-widget")
    label = page.get_by_text(re.compile(r"v[ée]rifier que vous|verify you|i'?m a human", re.I))
    try:
        widget.or_(label).first.wait_for(state="visible", timeout=8000)
    except PWTimeout:
        print("未发现验证码，跳过")
        return
    # 点击复选框（优先点组件内部的 checkbox，找不到就点文字/整个组件）
    inner = page.locator("cap-widget .checkbox, cap-widget [role=checkbox]")
    target = inner.first if inner.count() else (label.first if label.count() else widget.first)
    target.click()
    print("已点击验证码，等待计算…")
    try:
        # Cap 完成后会往表单里写入隐藏字段 cap-token
        page.wait_for_function(
            """() => { const i = document.querySelector('input[name="cap-token"]');
                       return i && i.value && i.value.length > 0; }""",
            timeout=timeout,
        )
        print("验证码通过")
    except PWTimeout:
        # 兜底：有的版本字段名不同，再多等几秒
        print("未检测到 cap-token，兜底等待 8 秒")
        page.wait_for_timeout(8000)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="fr-FR", viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(30000)

        # 1. 登录
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        page.locator('input[type="email"], input[name="email"], input[name="username"]').first.fill(EMAIL)
        page.locator('input[type="password"]').first.fill(PASSWORD)
        solve_cap(page)
        page.locator('button[type="submit"], input[type="submit"]').first.click()
        try:
            page.wait_for_url(lambda u: "/login" not in u, timeout=30000)
        except PWTimeout:
            shot(page, "login_failed.png")
            print("登录失败：可能账号密码错误或有验证码", file=sys.stderr)
            sys.exit(1)
        print("登录成功:", page.url)
        page.wait_for_load_state("networkidle")
        shot(page, "after_login.png")

        # 2. 找续期按钮（当前页没有就尝试常见的服务列表页）
        def find_btn():
            return page.get_by_role("button", name=RENEW_RE).or_(
                page.get_by_role("link", name=RENEW_RE)
            )

        btn = find_btn()
        if btn.count() == 0:
            for path in ("/services", "/service", "/hosting", "/dashboard"):
                page.goto("https://dash.neoheberg.fr" + path, wait_until="networkidle")
                btn = find_btn()
                if btn.count():
                    break

        if btn.count() == 0:
            shot(page, "no_renew_button.png")
            print("未找到续期按钮（可能还没到可续期时间，或需要调整选择器）")
            sys.exit(0)

        # 3. 点击续期（支持多个服务）
        for i in range(btn.count()):
            b = btn.nth(i)
            print("点击:", b.inner_text().strip())
            b.click()
            solve_cap(page, timeout=30000)
            # 若有二次确认弹窗
            confirm = page.get_by_role("button", name=re.compile(r"confirm|oui|yes|ok|valider", re.I))
            if confirm.count():
                confirm.first.click()
            page.wait_for_load_state("networkidle")

        shot(page, "renewed.png")
        print("续期完成")
        browser.close()


if __name__ == "__main__":
    main()
