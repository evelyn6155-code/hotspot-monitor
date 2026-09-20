#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
财经热点采集器 - GitHub Actions 版
和本地版 collect.py 的区别只有一处: 跑之前先从阿里云 OSS 把数据库文件下载下来,
跑完之后把最新的数据库文件传回 OSS。这样每次 GitHub 给的都是一台全新机器,
也能接着上一次的数据继续写,不会归零。

需要在 GitHub 仓库的 Settings > Secrets and variables > Actions 里配置好:
  OSS_ACCESS_KEY_ID
  OSS_ACCESS_KEY_SECRET
  OSS_ENDPOINT      例如 https://oss-cn-shanghai.aliyuncs.com
  OSS_BUCKET        你的 bucket 名字
  OSS_KEY           数据库在 OSS 里存放的路径, 例如 hotspot/hotspots.db
"""

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
import oss2

DB_PATH = "hotspots.db"
CST = timezone(timedelta(hours=8))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# ---------------------------------------------------------------------------
# 源配置, 和本地版一样, 加新源只改这里
# ---------------------------------------------------------------------------
SOURCES = {
    "wallstreetcn": {
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
    """开跑前, 把上一次存在 OSS 里的数据库取下来。如果 OSS 上还没有, 就从空的开始。"""
    bucket = get_bucket()
    key = os.environ["OSS_KEY"]
    try:
        bucket.get_object_to_file(key, DB_PATH)
        print(f"从 OSS 下载数据库成功: {key}")
    except oss2.exceptions.NoSuchKey:
        print(f"OSS 上还没有 {key}, 这是第一次运行, 从空数据库开始")


def upload_db():
    """跑完后, 把最新的数据库传回 OSS, 供下一次运行接着用。"""
    bucket = get_bucket()
    key = os.environ["OSS_KEY"]
    bucket.put_object_from_file(key, DB_PATH)
    print(f"上传数据库到 OSS 成功: {key}")


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


def fetch(cfg):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    headers.update(cfg.get("headers") or {})
    resp = requests.get(cfg["url"], headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def extract(item, fields):
    out = {}
    for std, path in fields.items():
        out[std] = dig(item, path) if path else None
    return out


def collect_one(conn, name, cfg):
    data = fetch(cfg)
    items = dig(data, cfg["list_path"])
    if not isinstance(items, list):
        print(f"[{name}] 取不到列表, 跳过")
        return 0

    ts = now_str()
    cur = conn.cursor()
    new_count = 0

    for idx, raw in enumerate(items):
        rec = extract(raw, cfg["fields"])
        title = rec.get("title")
        if not title:
            continue
        fp = fingerprint(title)
        rank = idx + 1 if cfg.get("ranked") else None
        hot = rec.get("hot")

        cur.execute(
            "INSERT INTO snapshots(source,fingerprint,rank,hot,captured_at,raw) "
            "VALUES (?,?,?,?,?,?)",
            (name, fp, rank, str(hot) if hot is not None else None, ts,
             json.dumps(raw, ensure_ascii=False)),
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
    print(f"[{name}] {ts} 抓到 {len(items)} 条, 其中新出现 {new_count} 条")
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


if __name__ == "__main__":
    download_db()
    collect()
    upload_db()
