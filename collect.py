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


def fetch_em_rank(cfg):
    """东方财富个股人气榜: emappdata 取排名, push2 补股票名与涨跌幅。"""
    r = requests.post(
        "https://emappdata.eastmoney.com/stockrank/getAllCurrentList",
        json={"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
              "marketType": "", "pageNo": 1, "pageSize": 100},
        headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    rows = r.json().get("data") or []
    secids = []
    for x in rows:
        sc = x["sc"]
        secids.append(("1." if sc.startswith("SH") else "0.") + sc[2:])
    names = {}
    try:   # 腾讯行情: 取名称与涨跌幅
        syms = [x["sc"].lower() for x in rows]
        for i in range(0, len(syms), 50):
            t = requests.get("https://qt.gtimg.cn/q=" + ",".join(syms[i:i + 50]),
                             headers={"User-Agent": UA}, timeout=15).content.decode("gbk", "ignore")
            for m in re.finditer(r'v_\w{2}(\d{6})="([^"]*)"', t):
                p = m.group(2).split("~")
                if len(p) > 32 and p[1]:
                    try:
                        pct = float(p[32])
                    except ValueError:
                        pct = None
                    names[m.group(1)] = {"f14": p[1], "f3": pct}
        print(f"[东财人气榜] 腾讯行情补全名称 {len(names)}/{len(rows)}")
    except Exception as e:
        print(f"[东财人气榜] 取股票名失败, 退回显示代码: {type(e).__name__}: {e}")
    out = []
    for x in rows:
        code = x["sc"][2:]
        d = names.get(code) or {}
        pct = d.get("f3")
        title = d.get("f14") or code
        out.append({"title": f"{title}({code})",
                    "url": f"https://guba.eastmoney.com/list,{code}.html",
                    "id": code,
                    "hot": f"{pct}%" if isinstance(pct, (int, float)) else None})
    return out


def fetch_xq(cfg):
    """雪球: 先访问首页拿 cookie, 再请求接口 (未验证, 看日志)。"""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://xueqiu.com/"})
    if os.environ.get("XQ_COOKIE"):
        s.headers["Cookie"] = os.environ["XQ_COOKIE"]
    else:
        s.get("https://xueqiu.com/", timeout=15)
    r = s.get(cfg["xq_url"], timeout=20)
    if r.status_code != 200:
        print(f"[雪球] HTTP {r.status_code}: {r.text[:200]!r}")
        r.raise_for_status()
    return r.json()



def fetch_douyin(cfg):
    """抖音热榜: 依次尝试几个公开接口, 有 DOUYIN_COOKIE 时带上 (均未验证, 看日志)。"""
    h = {"User-Agent": UA, "Referer": "https://www.douyin.com/"}
    if os.environ.get("DOUYIN_COOKIE"):
        h["Cookie"] = os.environ["DOUYIN_COOKIE"]
    last = None
    for u in cfg["dy_urls"]:
        try:
            r = requests.get(u, headers=h, timeout=20)
            print(f"[抖音] {u.split('?')[0]} -> HTTP {r.status_code}, 开头 {r.text[:120]!r}")
            j = r.json()
            lst = dig(j, "word_list") or dig(j, "data.word_list")
            if lst:
                return {"word_list": lst}
        except Exception as e:
            last = e
            print(f"[抖音] {u.split('?')[0]} 失败: {type(e).__name__}: {e}")
    if last:
        raise last
    return {}

FETCHERS = {"douyin": fetch_douyin, "em_rank": fetch_em_rank, "xq": fetch_xq}

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
        "fields": {"title": "title", "url": "uri", "id": "id", "hot": "pageviews",
                   "pub": "display_time"},
        "ranked": True,
    },
    "wscn_hot_week": {
        "label": "华尔街见闻·周榜",
        "site": "华尔街见闻", "sub": "周榜",
        "enabled": True,
        "url": "https://api-one-wscn.awtmt.com/apiv1/content/articles/hot?period=all",
        "headers": {},
        "list_path": "data.week_items",
        "fields": {"title": "title", "url": "uri", "id": "id", "hot": "pageviews",
                   "pub": "display_time"},
        "ranked": True,
    },
    "sina_top": {
        "label": "新浪财经·点击排行",
        "site": "新浪财经", "sub": "点击日榜",
        "enabled": False,   # 新浪点击排行是前一日日榜, 非实时, 已按需求停用
        "url": ("https://top.finance.sina.com.cn/ws/GetTopDataList.php?top_type=day"
                "&top_cat=finance_0_suda&top_time={today}&top_show_num=30"
                "&top_order=DESC&js_var=all_1_data01"),
        "headers": {"Referer": "https://finance.sina.com.cn/"},
        "parser": "jsonvar",
        "list_path": "data",
        "fields": {"title": "title", "url": "url", "id": "id", "hot": "top_num",
                   "pub": "time"},
        "note_field": "top_time",   # 新浪榜单自带的榜单日期, 当天数据未生成时会回退到前一天
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
            "pub": "resource.display_time",
        },
        "ranked": False,
    },
    "toutiao_all": {
        "label": "头条·总榜",
        "site": "今日头条", "sub": "总榜",
        "enabled": True,
        "url": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
        "headers": {"Referer": "https://www.toutiao.com/"},
        "list_path": "data",
        "url_template": "https://www.toutiao.com/trending/{id}/",
        "fields": {
            "title": "Title",
            "url": "Url",
            "id": "ClusterIdStr",
            "hot": "HotValue",
        },
        "ranked": True,
    },
    "toutiao": {
        "label": "头条·总榜财经分类",
        "site": "今日头条", "sub": "总榜财经分类",
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
    "baidu_finance": {
        "label": "百度热搜·财经榜",
        "site": "百度热搜", "sub": "财经榜",
        "enabled": True,
        # 百度热搜官方公开页面的数据接口, 财经榜每 5 分钟更新一次
        "url": "https://top.baidu.com/api/board?platform=wise&tab=finance",
        "headers": {"Referer": "https://top.baidu.com/board?tab=finance"},
        "list_path": "data.cards.0.content.0.content",
        "fields": {"title": "word", "url": "url", "id": "word", "hot": None},
        "ranked": True,
    },
    "douyin": {
        "label": "抖音·热榜",
        "site": "抖音", "sub": "热榜",
        "enabled": True, "fetcher": "douyin",
        "dy_urls": [
            "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/",
            "https://aweme.snssdk.com/aweme/v1/hot/search/list/?detail_list=1&aid=1128",
            "https://www.douyin.com/aweme/v1/web/hot/search/list/?device_platform=webapp&aid=6383&channel=channel_pc_web&detail_list=1",
        ],
        "list_path": "word_list",
        "fields": {"title": "word", "url": None, "id": "word", "hot": "hot_value"},
        "ranked": True,
    },
    "em_rank": {
        "label": "东方财富·个股人气榜",
        "site": "东方财富", "sub": "个股人气榜",
        "enabled": True, "fetcher": "em_rank",
        "list_path": "", "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot"},
        "ranked": True,
    },
    "ths_hour": {
        "label": "同花顺·热股榜(小时)",
        "site": "同花顺", "sub": "热股榜·小时",
        "enabled": True,
        "url": "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock?stock_type=a&type=hour&list_type=normal",
        "list_path": "data.stock_list",
        "fields": {"title": "name", "url": "url", "id": "code", "hot": "rate"},
        "url_template": "https://stockpage.10jqka.com.cn/{id}/",
        "ranked": True,
    },
    "ths_day": {
        "label": "同花顺·热股榜(日)",
        "site": "同花顺", "sub": "热股榜·日",
        "enabled": True,
        "url": "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock?stock_type=a&type=day&list_type=normal",
        "list_path": "data.stock_list",
        "fields": {"title": "name", "url": "url", "id": "code", "hot": "rate"},
        "url_template": "https://stockpage.10jqka.com.cn/{id}/",
        "ranked": True,
    },
    "xq_stock": {
        "label": "雪球·热股榜",
        "site": "雪球", "sub": "热股榜",
        "enabled": True, "fetcher": "xq",
        "xq_url": "https://stock.xueqiu.com/v5/stock/hot_stock/list.json?size=50&_type=10&type=10",
        "list_path": "data.items",
        "fields": {"title": "name", "url": "url", "id": "symbol", "hot": "value"},
        "url_template": "https://xueqiu.com/S/{id}",
        "ranked": True,
    },
    "weibo": {
        "label": "微博热搜",
        "site": "微博", "sub": "热搜",
        "enabled": bool(os.environ.get("WEIBO_COOKIE")),   # 需要在 GitHub Secrets 里放你自己账号的 cookie, 没配就不抓
        "url": "https://s.weibo.com/top/summary?cate=realtimehot",
        "headers": {"Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Referer": "https://s.weibo.com/"},
        "parser": "weibo",
        "cookie_env": "WEIBO_COOKIE",
        "list_path": "",
        "fields": STD_FIELDS,
        "ranked": True,
    },
}

# 页面上网站标签的显示顺序; 没列出的网站排在最后
SITE_ORDER = ["微博", "抖音", "今日头条", "百度热搜", "东方财富", "同花顺", "雪球", "华尔街见闻"]

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
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)")]
    if "pub_time" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN pub_time TEXT")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (source TEXT PRIMARY KEY, note TEXT)")
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


def norm_pub(v):
    """把各来源的发布时间统一成 'YYYY-MM-DD HH:MM:SS' (北京时间)。"""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            return datetime.fromtimestamp(int(v), CST).strftime("%Y-%m-%d %H:%M:%S")
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(str(v)).astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def fingerprint(title):
    norm = re.sub(r"[\s\W_]+", "", str(title), flags=re.UNICODE).lower()
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:16]


def fetch(name, cfg):
    if cfg.get("fetcher"):
        return FETCHERS[cfg["fetcher"]](cfg)
    headers = {"User-Agent": UA, "Accept": "application/json"}
    headers.update(cfg.get("headers") or {})
    if cfg.get("cookie_env") and os.environ.get(cfg["cookie_env"]):
        headers["Cookie"] = os.environ[cfg["cookie_env"]]
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
    if isinstance(data, dict):
        print(f"[{name}] 顶层字段: " + ", ".join(
            f"{k}({len(v) if hasattr(v, '__len__') else type(v).__name__})" for k, v in data.items()))
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
        pub = norm_pub(rec.get("pub"))
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
                "first_seen,last_seen,best_rank,pub_time) VALUES (?,?,?,?,?,?,?,?,?)",
                (name, fp, str(title), rec.get("url"),
                 str(rec.get("id")) if rec.get("id") is not None else None,
                 ts, ts, rank, pub),
            )
            new_count += 1
        else:
            item_id, best = row
            if rank is not None:
                best = rank if best is None else min(best, rank)
            cur.execute(
                "UPDATE items SET last_seen=?, best_rank=?, seen_count=seen_count+1, "
                "pub_time=COALESCE(?, pub_time) WHERE id=?",
                (ts, best, pub, item_id),
            )

    nf = cfg.get("note_field")
    if nf:
        vals = [str(dig(r, nf)) for r in items if dig(r, nf)]
        if vals:
            top = max(set(vals), key=vals.count)
            cur.execute("INSERT OR REPLACE INTO meta(source,note) VALUES (?,?)", (name, top))
            print(f"[{name}] 榜单日期 {top}")
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
            "SELECT s.rank, s.hot, i.title, i.url, i.first_seen, i.seen_count, i.pub_time "
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
            "rank_date": (cur.execute("SELECT note FROM meta WHERE source=?", (name,)).fetchone() or [None])[0],
            "items": [{"rank": r[0], "hot": r[1], "title": r[2], "url": r[3],
                       "first_seen": r[4], "new": r[5] == 1, "pub": r[6]} for r in rows],
        })
    conn.close()
    def _key(x):
        site = x.get("site")
        return SITE_ORDER.index(site) if site in SITE_ORDER else len(SITE_ORDER)
    out["sources"].sort(key=_key)   # 稳定排序, 同一网站内保持原来的先后
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
