#!/usr/bin/env python3
"""
奢侈品补货监控 (Luxury restock monitor)

检测逻辑：
  页面里"有货按钮"和"没货按钮"一直同时存在于 HTML 中，网站只是用 CSS 把其中一个藏起来。
  所以本脚本不是"搜文字"，而是让真实浏览器 (Playwright + Chromium) 渲染页面，
  再逐个检查按钮是否真的显示在屏幕上（没被 display:none / visibility:hidden /
  opacity:0 / 0 尺寸 / 禁用 等方式隐藏）。

退出码：
  0  检查完成（有货已通知，或确认没货）
  1  两个按钮都看不到 / 页面打不开（已保存截图 screenshot.png）
  3  有货，但通知发送失败

用法：
  python monitor.py                 # 正常检测
  python monitor.py --test-email    # 只发一封测试邮件，不打开商品页
  python monitor.py --test-bark     # 只发一条测试 Bark 警报（测试静音/睡眠模式能否响）

所有配置都从环境变量读取（在 GitHub 上由 Secrets 注入，代码里不写任何密码）：
  PRODUCT_URL          商品页链接
  IN_STOCK_TEXT        有货按钮文字，例如 ADD TO BAG
  OUT_OF_STOCK_TEXT    没货按钮文字，例如 ORDER BY PHONE
  BARK_KEY             Bark app 里的 key
  GMAIL_USER           你的 Gmail 地址（发件人）
  GMAIL_APP_PASSWORD   Gmail 应用专用密码（16 位）
  MAIL_TO              收件邮箱（可选，不填就发给 GMAIL_USER 自己）
"""

from __future__ import annotations

import argparse
import os
import re
import smtplib
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

SCREENSHOT_PATH = "screenshot.png"
PAGE_TIMEOUT_MS = 60_000      # 页面最多等 60 秒
BUTTON_WAIT_MS = 20_000       # 按钮最多等 20 秒出现
# 只认页面顶部这么多像素以内的按钮（主商品购买区）。下方推荐商品的按钮不算。
# 如果你的商品页购买按钮很靠下，可在 Secrets/Variables 里设 MAX_BUTTON_Y=4000 调大；设 0 表示不限制。
MAX_BUTTON_Y = int(os.environ.get("MAX_BUTTON_Y", "2500") or 0)

# 可能被做成"按钮"的元素类型
CLICKABLE = "button, a, [role='button'], input[type='submit'], input[type='button']"


# ----------------------------------------------------------------- 工具函数
def env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        print(f"❌ 缺少配置：{name}（请在 GitHub → Settings → Secrets 里添加）")
        sys.exit(1)
    return value


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC] {msg}", flush=True)


def text_pattern(text: str) -> re.Pattern:
    """忽略大小写、忽略多余空格地匹配按钮文字。"""
    words = [re.escape(w) for w in text.split()]
    return re.compile(r"\s*".join(["^"] + [r"\s+".join(words)] + [r"$"]), re.I)


# ----------------------------------------------------------------- 可见性判断
# 在浏览器里执行：判断元素是否"人眼真的看得到、能点"
VISIBLE_JS = """
(el, maxY) => {
  if (!el.isConnected) return false;
  // 1) 浏览器原生可见性检查（含 display/visibility/opacity/content-visibility）
  if (el.checkVisibility && !el.checkVisibility({
        checkOpacity: true, checkVisibilityCSS: true, contentVisibilityAuto: true
      })) return false;
  // 2) 自己和所有祖先都不能被隐藏
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    const s = getComputedStyle(n);
    if (s.display === 'none' || s.visibility === 'hidden' || s.visibility === 'collapse'
        || parseFloat(s.opacity) === 0) return false;
    if (n.hasAttribute('hidden') || n.getAttribute('aria-hidden') === 'true') return false;
  }
  // 3) 要有实际尺寸，且不能被移到页面外面（常见的 left:-9999px 藏法）
  const r = el.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return false;
  const docW = Math.max(document.documentElement.scrollWidth, window.innerWidth);
  if (r.right <= 0 || r.left >= docW || r.bottom + window.scrollY <= 0) return false;
  // 只看页面上半部分的主购买区，避免把"猜你喜欢"推荐商品里的按钮误判为有货
  if (maxY > 0 && r.top + window.scrollY > maxY) return false;
  // 4) 不能是被裁剪成 1px 的"屏幕阅读器专用"元素
  const s = getComputedStyle(el);
  if (s.clip === 'rect(0px, 0px, 0px, 0px)' || s.clipPath === 'inset(50%)') return false;
  // 5) 禁用的按钮不算
  if (el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
  return true;
}
"""


def find_visible(page, text: str) -> bool:
    """页面上是否有【真正可见】且文字匹配的按钮。"""
    pat = text_pattern(text)
    candidates = []
    # a) 按无障碍名称找按钮/链接（最准）
    candidates += page.get_by_role("button", name=pat).all()
    candidates += page.get_by_role("link", name=pat).all()
    # b) 文字写在按钮内部 <span> 里的情况
    candidates += page.locator(CLICKABLE).filter(has_text=pat).all()
    # c) <input value="ADD TO BAG">
    for el in page.locator("input[type='submit'], input[type='button']").all():
        try:
            if pat.search((el.get_attribute("value") or "").strip()):
                candidates.append(el)
        except Exception:
            pass
    # d) 用 <div> 假装按钮的网站：直接按文字找
    candidates += page.get_by_text(pat).all()

    for el in candidates:
        try:
            if el.is_visible() and el.evaluate(VISIBLE_JS, MAX_BUTTON_Y):
                return True
        except Exception:
            continue  # 元素在检查过程中被网页移除，跳过
    return False


# ----------------------------------------------------------------- 通知
def send_email(subject: str, body: str) -> None:
    user = env("GMAIL_USER")
    password = env("GMAIL_APP_PASSWORD").replace(" ", "")
    to = env("MAIL_TO", required=False) or user

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    log(f"📧 邮件已发送到 {to}")


def send_bark(title: str, body: str, url: str = "") -> None:
    key = env("BARK_KEY")
    # 兼容用户把整条 https://api.day.app/xxxx/ 链接粘进来的情况
    key = key.rstrip("/").split("/")[-1]
    q = lambda s: urllib.parse.quote(s, safe="")
    params = {
        "level": "critical",   # 重要警告：无视静音、勿扰、睡眠模式
        "volume": "10",        # 重要警告音量 0-10
        "call": "1",           # 铃声重复响 30 秒
        "group": "restock",
    }
    if url:
        params["url"] = url    # 点通知直接打开商品页
    full = f"https://api.day.app/{q(key)}/{q(title)}/{q(body)}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"User-Agent": "restock-monitor"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = resp.read().decode("utf-8", "ignore")
    if '"code":200' not in result.replace(" ", ""):
        raise RuntimeError(f"Bark 返回异常：{result}")
    log("🔔 Bark 警报已发送")


def notify_in_stock(url: str, text: str) -> bool:
    """有货：Bark + 邮件，两者独立，一个失败不影响另一个。返回是否全部成功。"""
    ok = True
    title = "🚨 补货了！快去买！"
    body = f"「{text}」按钮已出现，点开立刻下单"
    try:
        send_bark(title, body, url)
    except Exception as e:
        ok = False
        log(f"❌ Bark 发送失败：{e}")
    try:
        send_email(
            f"🚨 补货提醒：{text} 已可点击",
            f"监控的商品现在有货了！\n\n链接：{url}\n\n检测时间：{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n",
        )
    except Exception as e:
        ok = False
        log(f"❌ 邮件发送失败：{e}")
    return ok


# ----------------------------------------------------------------- 主检测流程
def check_stock() -> int:
    from playwright.sync_api import sync_playwright

    url = env("PRODUCT_URL")
    in_text = env("IN_STOCK_TEXT")
    out_text = env("OUT_OF_STOCK_TEXT")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()

        try:
            log(f"打开页面：{url}")
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass  # 有的网站一直有网络请求，没关系

            # 每秒检查一次，最多 20 秒。网页常常先显示默认按钮、再用 JS 刷新库存，
            # 所以要求"连续 4 次（约 3 秒）结果不变"才算数，避免误报或漏报。
            in_stock = out_of_stock = False
            last, stable = None, 0
            for _ in range(BUTTON_WAIT_MS // 1000):
                in_stock = find_visible(page, in_text)
                out_of_stock = find_visible(page, out_text)
                state = (in_stock, out_of_stock)
                stable = stable + 1 if state == last else 0
                last = state
                if (in_stock or out_of_stock) and stable >= 3:
                    break
                page.wait_for_timeout(1000)
        except Exception as e:
            log(f"❌ 页面加载出错：{e}")
            _screenshot(page)
            browser.close()
            return 1

        if in_stock:
            log(f"✅ 有货！「{in_text}」按钮可见")
            browser.close()
            return 0 if notify_in_stock(url, in_text) else 3

        if out_of_stock:
            log(f"⏳ 没货：「{out_text}」按钮可见，继续等待")
            browser.close()
            return 0

        log(f"⚠️ 两个按钮都看不到（可能被网站拦截、页面改版或按钮文字填错）")
        log(f"   页面标题：{page.title()!r}")
        _screenshot(page)
        browser.close()
        return 1


def _screenshot(page) -> None:
    try:
        page.screenshot(path=SCREENSHOT_PATH, full_page=True)
        log(f"📸 截图已保存：{SCREENSHOT_PATH}")
    except Exception as e:
        log(f"截图失败：{e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="奢侈品补货监控")
    parser.add_argument("--test-email", action="store_true", help="只发送一封测试邮件")
    parser.add_argument("--test-bark", action="store_true", help="只发送一条测试 Bark 警报")
    args = parser.parse_args()

    if args.test_email or args.test_bark:
        code = 0
        if args.test_email:
            try:
                send_email(
                    "✅ 补货监控：测试邮件",
                    "收到这封邮件，说明 Gmail 设置正确。\n有货时你会收到类似的邮件。",
                )
            except Exception as e:
                log(f"❌ 测试邮件发送失败：{e}")
                code = 1
        if args.test_bark:
            try:
                send_bark("✅ 补货监控测试", "如果手机静音时也响了，说明设置成功",
                          os.environ.get("PRODUCT_URL", ""))
            except Exception as e:
                log(f"❌ 测试 Bark 发送失败：{e}")
                code = 1
        return code

    return check_stock()


if __name__ == "__main__":
    sys.exit(main())
