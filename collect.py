#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
财经热点采集器 - GitHub Actions 版

流程: 从 OSS 下载数据库 -> 逐个源采集 -> 导出前端用的 latest.json 并上传 -> 上传 web/index.html -> 把数据库传回 OSS。
数据库对象权限为私有, latest.json 和页面为公共读(网站要读)。

需要的 Secrets: OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / OSS_ENDPOINT / OSS_BUCKET
环境变量 OSS_KEY 为数据库路径, 例如 hotspot/hotspots.db
"""

import hashlib
import html as htmllib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
import oss2

DB_PATH = "hotspots.db"
CST = timezone(timedelta(hours=8))
SITE_PREFIX = "hotspot/"          # 前端和 latest.json 放在 OSS 的这个目录下

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


# ---------------------------------------------------------------------------
# HTML 源的解析函数: 输入页面文本, 输出 [{title,url,id,hot}, ...]
# ---------------------------------------------------------------------------
def parse_weibo(text):
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S):
        m_rank = re.search(r'<td class="td-01[^"]*">\s*(\d+)\s*</td>', tr)
        m_a = re.search(r'<td class="td-02">\s*<a href="([^"]*)"[^>]*>(.*?)</a>(.*?)</td>',
                        tr, re.S)
        if not m_rank or not m_a:      # 没有序号的是置顶词, 跳过
            continue
        title = htmllib.unescape(re.sub(r"<[^>]+>", "", m_a.group(2))).strip()
        extra = re.sub(r"<[^>]+>", " ", m_a.group(3))
        nums = re.findall(r"\d+", extra)
        out.append({
            "title": title,
            "url": urljoin("https://s.weibo.com", m_a.group(1)),
            "id": None,
            "hot": nums[-1] if nums else None,
        })
    return out


def parse_jsonvar(text):
    """解析形如 `var x = {...};` 的 JS 变量赋值, 返回其中的 JSON 对象。"""
    m = re.search(r"=\s*(\{.*\})\s*;?\s*$", text.strip(), re.S)
    return json.loads(m.group(1)) if m else None


PARSERS = {"weibo": parse_weibo, "jsonvar": parse_jsonvar}

STD_FIELDS = {"title": "title", "url": "url", "id": "id", "hot": "hot"}

# ---------------------------------------------------------------------------
# 源配置: 加新源只改这里
# ---------------------------------------------------------------------------
SOURCES = {
    "wscn_hot_day": {
        "label": "华尔街见闻·日榜",
        "site": "华尔街见闻", "sub": "日榜",
        "enabled": True,
        "url": "https://api-one-wscn.awtmt.com/apiv1/content/articles/hot?period=all",
        "headers": {},
        "list_path": "data.day_items",
        "fields": {"title": "title", "url": "uri", "id": "id", "hot": "pageviews"},
        "ranked": True,
    },
    "wscn_hot_week": {
        "label": "华尔街见闻·周榜",
        "site": "华尔街见闻", "sub": "周榜",
        "enabled": True,
        "url": "https://api-one-wscn.awtmt.com/apiv1/content/articles/hot?period=all",
        "headers": {},
        "list_path": "data.week_items",
        "fields": {"title": "title", "url": "uri", "id": "id", "hot": "pageviews"},
        "ranked": True,
    },
    "sina_top": {
        "label": "新浪财经·点击排行",
        "site": "新浪财经", "sub": "点击排行",
        "enabled": True,
        "url": ("https://top.finance.sina.com.cn/ws/GetTopDataList.php?top_type=day"
                "&top_cat=finance_0_suda&top_time={today}&top_show_num=30"
                "&top_order=DESC&js_var=all_1_data01"),
        "headers": {"Referer": "https://finance.sina.com.cn/"},
        "parser": "jsonvar",
        "list_path": "data",
        "fields": {"title": "title", "url": "url", "id": "id", "hot": "top_num"},
        "ranked": True,
    },
    "wallstreetcn": {
        "label": "华尔街见闻·资讯流",
        "site": "华尔街见闻", "sub": "资讯流",
        "enabled": True,
        "url": "https://api.wscn.net/apiv1/content/information-flow?channel=global&accept=article,live,chart&limit=30",
        "headers": {},
        "list_path": "data.items",
        "fields": {
            "title": "resource.title",
            "url": "resource.uri",
            "id": "resource.id",
            "hot": None,
        },
        "ranked": False,
    },
    "toutiao": {
        "label": "头条·财经热榜",
        "site": "今日头条", "sub": "财经热榜",
        "enabled": True,
        "url": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
        "headers": {"Referer": "https://www.toutiao.com/"},
        "list_path": "data",
        # 总榜里每条带 InterestCategory, 只留财经类; 排名保留它在总榜里的位置
        "url_template": "https://www.toutiao.com/trending/{id}/",
        "filter": {"path": "InterestCategory", "any_of": ["finance"]},
        "fields": {
            "title": "Title",
            "url": "Url",
            "id": "ClusterIdStr",
            "hot": "HotValue",
        },
        "ranked": True,
    },
    "weibo": {
        "label": "微博热搜",
        "site": "微博", "sub": "热搜",
        "enabled": False,   # GitHub 机房 IP 会被新浪访客系统拦截, 待接入合规来源后再启用
        "url": "https://s.weibo.com/top/summary?cate=realtimehot",
        "headers": {"Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Referer": "https://s.weibo.com/"},
        "parser": "weibo",
        "list_path": "",
        "fields": STD_FIELDS,
        "ranked": True,
    },
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    rank          INTEGER,
    hot           TEXT,
    captured_at   TEXT NOT NULL,
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_fp   ON snapshots(source, fingerprint);
CREATE INDEX IF NOT EXISTS idx_snap_time ON snapshots(captured_at);

CREATE TABLE IF NOT EXISTS items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,
    title          TEXT NOT NULL,
    url            TEXT,
    source_item_id TEXT,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    best_rank      INTEGER,
    seen_count     INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_items_first ON items(first_seen);
CREATE INDEX IF NOT EXISTS idx_items_fp    ON items(fingerprint);
"""


def now_str():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def get_bucket():
    auth = oss2.Auth(os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"])
    return oss2.Bucket(auth, os.environ["OSS_ENDPOINT"], os.environ["OSS_BUCKET"])


def download_db():
    bucket = get_bucket()
    key = os.environ["OSS_KEY"]
    try:
        bucket.get_object_to_file(key, DB_PATH)
        print(f"从 OSS 下载数据库成功: {key}")
    except oss2.exceptions.NoSuchKey:
        print(f"OSS 上还没有 {key}, 从空数据库开始")


def upload_db():
    bucket = get_bucket()
    key = os.environ["OSS_KEY"]
    bucket.put_object_from_file(key, DB_PATH, headers={"x-oss-object-acl": "private"})
    print(f"上传数据库到 OSS 成功(私有): {key}")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def dig(obj, path):
    if path is None or path == "":
        return obj
    cur = obj
    for key in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and key.isdigit():
            idx = int(key)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def fingerprint(title):
    norm = re.sub(r"[\s\W_]+", "", str(title), flags=re.UNICODE).lower()
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:16]


def fetch(name, cfg):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    headers.update(cfg.get("headers") or {})
    url = cfg["url"].replace("{today}", datetime.now(CST).strftime("%Y%m%d"))
    resp = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            break
        except requests.exceptions.RequestException as e:
            print(f"[{name}] 第 {attempt + 1} 次请求失败: {type(e).__name__}")
            if attempt == 2:
                raise
            time.sleep(2)
    if resp.status_code != 200:
        print(f"[{name}] HTTP {resp.status_code}, 响应开头: {resp.text[:200]!r}")
        resp.raise_for_status()
    if cfg.get("parser"):
        data = PARSERS[cfg["parser"]](resp.text)
        if not data:
            print(f"[{name}] 解析结果为空, 页面长度 {len(resp.text)}, 开头: {resp.text[:300]!r}")
        return data
    try:
        return resp.json()
    except ValueError:
        print(f"[{name}] 返回的不是 JSON, 开头: {resp.text[:300]!r}")
        raise


def extract(item, fields):
    return {std: (dig(item, path) if path else None) for std, path in fields.items()}


def collect_one(conn, name, cfg):
    data = fetch(name, cfg)
    items = dig(data, cfg["list_path"])
    if not isinstance(items, list):
        print(f"[{name}] 取不到列表, 跳过")
        return 0

    ts = now_str()
    cur = conn.cursor()
    new_count = 0
    kept = 0

    flt = cfg.get("filter")
    if flt:
        from collections import Counter
        cats = Counter()
        for raw in items:
            v = dig(raw, flt["path"])
            for c in (v if isinstance(v, list) else [v]):
                cats[str(c)] += 1
        print(f"[{name}] 总榜 {len(items)} 条, 分类分布: {dict(cats)}")

    for idx, raw in enumerate(items):
        if flt:
            v = dig(raw, flt["path"])
            vals = set(str(c) for c in (v if isinstance(v, list) else [v]))
            if not vals & set(flt["any_of"]):
                continue
        rec = extract(raw, cfg["fields"])
        if cfg.get("url_template") and rec.get("id"):
            rec["url"] = cfg["url_template"].format(id=rec["id"])
        title = rec.get("title")
        if not title:
            continue
        kept += 1
        fp = fingerprint(title)
        rank = idx + 1 if cfg.get("ranked") else None
        hot = rec.get("hot")
        if isinstance(hot, str):
            hot = hot.replace(",", "").strip()

        cur.execute(
            "INSERT INTO snapshots(source,fingerprint,rank,hot,captured_at,raw) "
            "VALUES (?,?,?,?,?,?)",
            (name, fp, rank, str(hot) if hot is not None else None, ts,
             json.dumps(rec, ensure_ascii=False)),
        )

        cur.execute("SELECT id,best_rank FROM items WHERE source=? AND fingerprint=?",
                    (name, fp))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO items(source,fingerprint,title,url,source_item_id,"
                "first_seen,last_seen,best_rank) VALUES (?,?,?,?,?,?,?,?)",
                (name, fp, str(title), rec.get("url"),
                 str(rec.get("id")) if rec.get("id") is not None else None,
                 ts, ts, rank),
            )
            new_count += 1
        else:
            item_id, best = row
            if rank is not None:
                best = rank if best is None else min(best, rank)
            cur.execute(
                "UPDATE items SET last_seen=?, best_rank=?, seen_count=seen_count+1 "
                "WHERE id=?",
                (ts, best, item_id),
            )

    conn.commit()
    print(f"[{name}] {ts} 抓到 {kept} 条, 其中新出现 {new_count} 条")
    return new_count


def collect():
    conn = db()
    for name, cfg in SOURCES.items():
        if not cfg.get("enabled"):
            continue
        try:
            collect_one(conn, name, cfg)
        except Exception as e:
            print(f"[{name}] 失败: {type(e).__name__}: {e}")
        time.sleep(1)
    conn.close()


def export_latest():
    """把每个源最近一次采集的榜单导出成 JSON, 供前端读取。"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    out = {"generated_at": now_str(), "sources": []}
    for name, cfg in SOURCES.items():
        if not cfg.get("enabled"):
            continue
        last = cur.execute("SELECT MAX(captured_at) FROM snapshots WHERE source=?",
                           (name,)).fetchone()[0]
        if not last:
            continue
        rows = cur.execute(
            "SELECT s.rank, s.hot, i.title, i.url, i.first_seen, i.seen_count "
            "FROM snapshots s JOIN items i ON i.source=s.source AND i.fingerprint=s.fingerprint "
            "WHERE s.source=? AND s.captured_at=? "
            "ORDER BY COALESCE(s.rank, 9999), s.id", (name, last)).fetchall()
        out["sources"].append({
            "id": name,
            "label": cfg.get("label", name),
            "site": cfg.get("site", cfg.get("label", name)),
            "sub": cfg.get("sub", ""),
            "ranked": bool(cfg.get("ranked")),
            "captured_at": last,
            "items": [{"rank": r[0], "hot": r[1], "title": r[2], "url": r[3],
                       "first_seen": r[4], "new": r[5] == 1} for r in rows],
        })
    conn.close()
    return out


def upload_site():
    bucket = get_bucket()
    data = export_latest()
    bucket.put_object(
        SITE_PREFIX + "latest.json",
        json.dumps(data, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Cache-Control": "no-cache", "x-oss-object-acl": "public-read"},
    )
    print(f"上传 latest.json 成功, 共 {len(data['sources'])} 个源")
    page = os.path.join("web", "index.html")
    if os.path.exists(page):
        bucket.put_object_from_file(
            SITE_PREFIX + "index.html", page,
            headers={"Content-Type": "text/html; charset=utf-8",
                     "Cache-Control": "no-cache", "x-oss-object-acl": "public-read"},
        )
        print("上传 index.html 成功")


if __name__ == "__main__":
    download_db()
    collect()
    try:
        upload_site()
    except Exception as e:
        print(f"上传站点文件失败: {type(e).__name__}: {e}")
    upload_db()
