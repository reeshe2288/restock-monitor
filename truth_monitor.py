#!/usr/bin/env python3
"""
Trump Truth Social 新帖监控

每次运行：抓取 @realDonaldTrump 最新的帖子，和上次记住的"最新帖子 ID"比较，
有新帖就推送 Bark（手机/电脑）+ 发 Gmail 邮件。

数据来源（自动切换）：
  1. Truth Social 官方接口（最快，实时）
  2. trumpstruth.org 存档 RSS（官方接口被拦截时备用，通常延迟几秒到几分钟）
两边用的是同一个帖子 ID，切换来源不会重复提醒。

状态文件 truth_state.json 记录上次看到的最新帖子 ID（GitHub 用缓存保存它）。
第一次运行只记录、不提醒，避免把几十条旧帖一次性推给你。

用法：
  python truth_monitor.py              # 正常检测
  python truth_monitor.py --test       # 不看状态，把最新 1 条当新帖推送一次（测试通知）

配置（GitHub Secrets，大部分和补货监控共用）：
  TRUTH_BARK_KEYS      手机和 Mac 的 Bark key，用英文逗号分隔（例如 手机key,Mac的key）；
                       不填就只用补货监控的 BARK_KEY（只推手机）
  GMAIL_USER / GMAIL_APP_PASSWORD / MAIL_TO   同补货监控
  TRUTH_BARK_LEVEL     （可选）白天的推送级别：timeSensitive（默认，有声音、能穿透专注模式）
                       / critical（重要警告，静音也响）/ active（普通通知）
  QUIET_HOURS          （可选）同补货监控，默认 23-9；这段时间只发静音的普通通知
  TRUTH_EMAIL          （可选）填 off 则不发邮件，只推 Bark
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from monitor import env, in_quiet_hours, log, send_email

ACCOUNT_ID = "107780257626128497"  # @realDonaldTrump
OFFICIAL_URL = f"https://truthsocial.com/api/v1/accounts/{ACCOUNT_ID}/statuses?limit=20"
ARCHIVE_URL = "https://www.trumpstruth.org/feed?limit=20"
STATE_FILE = "truth_state.json"
BATCH_LIMIT = 4  # 一次超过这么多条新帖，就合并成一条推送，避免手机连响
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
NS = {"truth": "https://truthsocial.com/ns", "archive": "https://www.trumpstruth.org/rss-extensions"}


# ----------------------------------------------------------------- 抓取
def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, application/rss+xml, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", "ignore")


def clean(text: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>", "\n", text or "", flags=re.I)
    text = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>.*?</a>', r" \1 ", text, flags=re.S)  # 链接显示完整网址
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{2,}", "\n", text)).strip()


def parse_official(raw: str) -> list[dict]:
    posts = []
    for s in json.loads(raw):
        rb = s.get("reblog")
        body = clean((rb or s).get("content", ""))
        media = len((rb or s).get("media_attachments") or [])
        if rb:
            kind = f"ReTruth @{(rb.get('account') or {}).get('acct', '')}"
        elif s.get("in_reply_to_id"):
            kind = "回复"
        else:
            kind = "原帖"
        posts.append({
            "id": str(s["id"]),
            "time": s.get("created_at", ""),
            "url": s.get("url") or f"https://truthsocial.com/@realDonaldTrump/{s['id']}",
            "text": body,
            "media": media,
            "kind": kind,
        })
    return posts


def parse_archive(raw: str) -> list[dict]:
    posts = []
    root = ET.fromstring(raw)
    for item in root.iter("item"):
        oid = item.findtext("truth:originalId", default="", namespaces=NS).strip()
        if not oid:
            continue
        desc = item.findtext("description", default="")
        title = item.findtext("title", default="")
        text = clean(desc) or clean(title)
        if re.fullmatch(r"(Image |Video )?[Pp]ost from .*", title or "") and not clean(desc):
            text = ""
        ptype = item.findtext("archive:postType", default="original", namespaces=NS)
        posts.append({
            "id": oid,
            "time": item.findtext("pubDate", default=""),
            "url": item.findtext("truth:originalUrl", default="", namespaces=NS)
                   or f"https://truthsocial.com/@realDonaldTrump/{oid}",
            "text": text,
            "media": 1 if "View i" in desc or "attachments/" in desc else 0,
            "kind": "原帖" if ptype == "original" else ptype,
        })
    return posts


def fetch_posts() -> tuple[list[dict], str]:
    errors = []
    for name, url, parser in (("Truth Social 官方", OFFICIAL_URL, parse_official),
                              ("trumpstruth.org 存档", ARCHIVE_URL, parse_archive)):
        try:
            posts = parser(_get(url))
            if posts:
                return posts, name
            errors.append(f"{name}: 返回为空")
        except Exception as e:
            errors.append(f"{name}: {e}")
    raise RuntimeError("两个来源都读不到：" + "；".join(errors))


# ----------------------------------------------------------------- 状态
def load_state() -> str | None:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return str(json.load(f).get("last_id") or "") or None
    except (FileNotFoundError, ValueError):
        return None


def save_state(last_id: str) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_id": last_id,
                   "updated": datetime.now(timezone.utc).isoformat()}, f)


# ----------------------------------------------------------------- 通知
def bark_push(title: str, body: str, url: str) -> bool:
    raw = os.environ.get("TRUTH_BARK_KEYS", "").strip() or env("BARK_KEY")
    keys = [k.strip().rstrip("/").split("/")[-1] for k in raw.split(",") if k.strip()]
    if in_quiet_hours():
        params = {"level": "passive"}  # 夜间：静默进通知中心，不亮屏不响
        note = "免打扰时段，已发静音通知"
    else:
        level = (os.environ.get("TRUTH_BARK_LEVEL", "").strip() or "timeSensitive")
        params = {"level": level, "sound": "bell"}
        if level == "critical":
            params["volume"] = "8"
        note = f"已推送（{level}）"
    params.update({"group": "trump", "url": url})
    q = lambda s: urllib.parse.quote(s, safe="")
    ok = True
    for key in keys:
        full = f"https://api.day.app/{q(key)}/{q(title)}/{q(body)}?{urllib.parse.urlencode(params)}"
        try:
            req = urllib.request.Request(full, headers={"User-Agent": "truth-monitor"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                res = resp.read().decode("utf-8", "ignore")
            if '"code":200' not in res.replace(" ", ""):
                raise RuntimeError(res)
        except Exception as e:
            ok = False
            log(f"❌ Bark（key …{key[-4:]}）发送失败：{e}")
    if ok:
        log(f"🔔 Bark {note}，共 {len(keys)} 台设备")
    return ok


def describe(p: dict) -> str:
    text = p["text"] or ("[图片/视频]" if p["media"] else "[链接/无文字]")
    if p["media"] and p["text"]:
        text += " [附图/视频]"
    return text


def notify(new_posts: list[dict]) -> bool:
    new_posts = sorted(new_posts, key=lambda p: int(p["id"]))  # 旧 → 新
    ok = True
    if len(new_posts) > BATCH_LIMIT:
        latest = new_posts[-1]
        ok &= bark_push(f"Trump 连发 {len(new_posts)} 条 Truth",
                        "最新：" + describe(latest)[:180], latest["url"])
    else:
        for p in new_posts:
            ok &= bark_push(f"Trump 发帖（{p['kind']}）", describe(p)[:220], p["url"])

    if (os.environ.get("TRUTH_EMAIL", "").strip().lower() not in ("off", "no", "0")):
        lines = []
        for p in new_posts:
            lines.append(f"[{p['kind']}] {p['time']}\n{describe(p)}\n{p['url']}\n")
        subject = (f"Trump Truth Social：{len(new_posts)} 条新帖" if len(new_posts) > 1
                   else f"Trump Truth Social：{describe(new_posts[0])[:60]}")
        try:
            send_email(subject, "\n".join(lines))
        except Exception as e:
            ok = False
            log(f"❌ 邮件发送失败：{e}")
    return ok


# ----------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="把最新 1 条当作新帖推送一次")
    args = ap.parse_args()

    try:
        posts, source = fetch_posts()
    except Exception as e:
        log(f"❌ {e}")
        return 1
    posts.sort(key=lambda p: int(p["id"]))
    newest = posts[-1]
    log(f"来源：{source}，读到 {len(posts)} 条，最新 {newest['id']}（{newest['time']}）")

    if args.test:
        log("🧪 测试模式：推送最新 1 条")
        return 0 if notify([newest]) else 3

    last = load_state()
    if last is None:
        save_state(newest["id"])
        log("📌 第一次运行：已记住当前最新帖子，从下一条新帖开始提醒")
        return 0

    new_posts = [p for p in posts if int(p["id"]) > int(last)]
    if not new_posts:
        log("⏳ 没有新帖")
        return 0

    log(f"🆕 {len(new_posts)} 条新帖")
    ok = notify(new_posts)
    save_state(newest["id"])  # 就算通知有失败也前进，避免下一轮重复轰炸
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
