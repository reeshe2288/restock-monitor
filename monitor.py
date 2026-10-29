#!/usr/bin/env python3
"""
奢侈品补货监控 (Luxury restock monitor)

检测逻辑：
  页面里"有货按钮"和"没货按钮"一直同时存在于 HTML 中，网站只是用 CSS 把其中一个藏起来。
  所以本脚本不是"搜文字"，而是让真实浏览器 (Playwright + Chromium) 渲染页面，
  再逐个检查按钮是否真的显示在屏幕上（没被 display:none / visibility:hidden /
  opacity:0 / 0 尺寸 / 禁用 等方式隐藏）。

有货验证：
  看到"有货按钮"后，脚本会真的点 ADD TO BAG，再点 PROCEED TO CHECKOUT。
  如果过程中出现 "...no longer available..." 提示，说明是假有货，不发通知。
  脚本只把商品放进一个一次性的空购物袋，不登录、不填地址、不付款。

退出码：
  0  检查完成（真有货已通知，或确认没货 / 假有货）
  1  两个按钮都看不到 / 页面打不开（已保存截图 screenshot.png）
  3  有货，但通知发送失败
  4  有货按钮可见、已发通知，但没能走完"加入购物袋→结账"验证（已保存截图，
     方便核对按钮文字；宁可多提醒一次也不漏掉）

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
  UNAVAILABLE_TEXT     （可选）加入购物袋/结账后出现就说明其实没货的提示语，
                       默认 "no longer available"（对应梵克雅宝的
                       "Kindly note, the creations in your shopping bag are no longer available"）
  CHECKOUT_TEXTS       （可选）去结账按钮文字，用 | 分隔，默认
                       "PROCEED TO CHECKOUT|CHECKOUT|VIEW SHOPPING BAG|VIEW BAG|SHOPPING BAG"
  QUIET_HOURS          （可选）免打扰时段，默认 23-9，即手机时间晚 11 点到早 9 点
                       这段时间里有货只发普通通知（不响铃、不穿透静音/睡眠），邮件照发
                       填 off 表示全天都用重要警告
  PHONE_TIMEZONE       （可选）手机所在时区，默认 America/New_York（自动处理夏令时）
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
from zoneinfo import ZoneInfo
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


def find_visible(page, text: str, max_y: int | None = None):
    """返回页面上第一个【真正可见】且文字匹配的按钮（找不到返回 None）。"""
    max_y = MAX_BUTTON_Y if max_y is None else max_y
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
            if el.is_visible() and el.evaluate(VISIBLE_JS, max_y):
                return el
        except Exception:
            continue  # 元素在检查过程中被网页移除，跳过
    return None


def visible_text_on_page(page, phrase: str) -> bool:
    """页面上是否【真正显示】了包含 phrase 的文字（隐藏在 HTML 里的不算）。"""
    pat = re.compile(r"\s+".join(re.escape(w) for w in phrase.split()), re.I)
    for el in page.get_by_text(pat).all():
        try:
            if el.is_visible() and el.evaluate(VISIBLE_JS, 0):
                return True
        except Exception:
            continue
    return False


# ----------------------------------------------------------------- 加入购物袋验证
def verify_in_cart(page, add_button) -> str:
    """
    点 ADD TO BAG → 点去结账，看会不会出现"已售罄"提示。
    返回 "available"（走到结账也没提示，真有货）
         "unavailable"（出现 no longer available，假有货）
         "unknown"（流程没走通，无法确认）
    """
    unavailable = os.environ.get("UNAVAILABLE_TEXT", "").strip() or "no longer available"
    checkout_texts = [
        t.strip() for t in (
            os.environ.get("CHECKOUT_TEXTS", "").strip()
            or "PROCEED TO CHECKOUT|CHECKOUT|VIEW SHOPPING BAG|VIEW BAG|SHOPPING BAG"
        ).split("|") if t.strip()
    ]

    def wait_for_message(seconds: int) -> bool:
        for _ in range(seconds):
            if visible_text_on_page(page, unavailable):
                return True
            page.wait_for_timeout(1000)
        return False

    log("🛍️ 点击 ADD TO BAG 验证是否真的能买")
    try:
        add_button.scroll_into_view_if_needed(timeout=5000)
        add_button.click(timeout=10_000)
    except Exception as e:
        log(f"⚠️ 点不了 ADD TO BAG：{e}")
        return "unknown"

    if wait_for_message(5):
        log(f"🚫 加入购物袋后出现「{unavailable}」，其实没货")
        return "unavailable"

    # 最多点 3 次：可能是 购物袋弹窗 → VIEW BAG → PROCEED TO CHECKOUT
    clicked_checkout = False
    for hop in range(3):
        target = None
        for _ in range(10):  # 每次最多等 10 秒按钮出现
            for t in checkout_texts:  # 按优先级：先找 PROCEED TO CHECKOUT
                target = find_visible(page, t, max_y=0)
                if target:
                    log(f"➡️ 点击「{t}」")
                    is_checkout = "CHECKOUT" in t.upper()
                    break
            if target or visible_text_on_page(page, unavailable):
                break
            page.wait_for_timeout(1000)

        if visible_text_on_page(page, unavailable):
            log(f"🚫 出现「{unavailable}」，其实没货")
            return "unavailable"
        if not target:
            break
        try:
            target.click(timeout=10_000)
        except Exception as e:
            log(f"⚠️ 点击失败：{e}")
            break
        try:
            page.wait_for_load_state("domcontentloaded", timeout=20_000)
        except Exception:
            pass
        if wait_for_message(6):
            log(f"🚫 结账时出现「{unavailable}」，其实没货")
            return "unavailable"
        if is_checkout:
            clicked_checkout = True
            break

    if clicked_checkout:
        log("✅ 已走到结账页，没有出现售罄提示，确认真有货")
        return "available"
    log("⚠️ 没找到去结账的按钮，无法完成验证")
    return "unknown"


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


def in_quiet_hours(now: datetime | None = None) -> bool:
    """现在是否处于免打扰时段（按手机时区计算，跨午夜也正确）。"""
    spec = (os.environ.get("QUIET_HOURS", "").strip() or "23-9").lower()
    if spec in ("off", "none", "0", "no"):
        return False
    try:
        start, end = (int(x) for x in spec.split("-"))
    except ValueError:
        log(f"⚠️ QUIET_HOURS 格式不对（应为 23-9 这样），已按 23-9 处理")
        start, end = 23, 9
    tz = ZoneInfo(os.environ.get("PHONE_TIMEZONE", "").strip() or "America/New_York")
    hour = (now or datetime.now(tz)).astimezone(tz).hour
    if start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


def send_bark(title: str, body: str, url: str = "", critical: bool | None = None) -> None:
    key = env("BARK_KEY")
    # 兼容用户把整条 https://api.day.app/xxxx/ 链接粘进来的情况
    key = key.rstrip("/").split("/")[-1]
    q = lambda s: urllib.parse.quote(s, safe="")
    if critical is None:
        critical = not in_quiet_hours()
    if critical:
        params = {
            "level": "critical",   # 重要警告：无视静音、勿扰、睡眠模式
            "volume": "10",        # 重要警告音量 0-10
            "call": "1",           # 铃声重复响 30 秒
        }
    else:
        # 免打扰时段：普通通知，静音/睡眠模式下不会响，醒来能在通知中心看到
        params = {"level": "active"}
    params["group"] = "restock"
    if url:
        params["url"] = url    # 点通知直接打开商品页
    full = f"https://api.day.app/{q(key)}/{q(title)}/{q(body)}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"User-Agent": "restock-monitor"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = resp.read().decode("utf-8", "ignore")
    if '"code":200' not in result.replace(" ", ""):
        raise RuntimeError(f"Bark 返回异常：{result}")
    log("🔔 Bark 重要警告已发送" if critical else "🔕 免打扰时段，Bark 已发普通通知（不响铃）")


def notify_in_stock(url: str, text: str, verified: bool = True) -> bool:
    """有货：Bark + 邮件，两者独立，一个失败不影响另一个。返回是否全部成功。"""
    ok = True
    title = "🚨 补货了！快去买！"
    if verified:
        body = "已成功加入购物袋并进入结账，点开立刻下单"
    else:
        body = f"「{text}」按钮已出现（未能自动验证结账），点开立刻看看"
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
            in_stock = out_of_stock = None
            last, stable = None, 0
            for _ in range(BUTTON_WAIT_MS // 1000):
                in_stock = find_visible(page, in_text)
                out_of_stock = find_visible(page, out_text)
                state = (bool(in_stock), bool(out_of_stock))
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
            log(f"👀「{in_text}」按钮可见，开始验证")
            try:
                result = verify_in_cart(page, in_stock)
            except Exception as e:
                log(f"⚠️ 验证过程出错：{e}")
                result = "unknown"
            if result == "unavailable":
                browser.close()
                return 0  # 假有货，不通知
            if result == "unknown":
                _screenshot(page)
            browser.close()
            if not notify_in_stock(url, in_text, verified=(result == "available")):
                return 3
            return 0 if result == "available" else 4

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
