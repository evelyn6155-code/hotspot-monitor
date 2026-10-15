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
from urllib.parse import urljoin, quote

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


def _qt_names(syms):
    """腾讯行情批量取名称与涨跌幅。syms 为腾讯代码 (sh600000/hk00700/usAAPL), 返回 {小写代码: (名称, 涨跌幅, 现价)}。"""
    res = {}
    for i in range(0, len(syms), 50):
        t = requests.get("https://qt.gtimg.cn/q=" + ",".join(syms[i:i + 50]),
                         headers={"User-Agent": UA}, timeout=15).content.decode("gbk", "ignore")
        for m in re.finditer(r'v_([^=]+)="([^"]*)"', t):
            p = m.group(2).split("~")
            if len(p) > 32 and p[1]:
                try:
                    pct = float(p[32])
                except ValueError:
                    pct = None
                res[m.group(1).lower().split(".")[0]] = (p[1], pct, p[3])
    return res


def fetch_em_rank(cfg):
    """东方财富人气榜 (A股/港股/美股/ETF): emappdata 取排名, 腾讯行情补名称与涨跌幅。"""
    r = requests.post(
        cfg.get("em_url", "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"),
        json={"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
              "marketType": cfg.get("market_type", ""), "pageNo": 1, "pageSize": 100},
        headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    rows = r.json().get("data") or []
    kind = cfg.get("kind", "a")
    items = []     # (展示代码, 腾讯代码, 链接)
    for x in rows:
        sc = x["sc"]
        if kind == "hk":
            code = sc.split("|")[-1]
            items.append((code, "hk" + code, f"https://quote.eastmoney.com/hk/{code}.html"))
        elif kind == "us":
            code = sc.split("|")[-1]
            items.append((code, "us" + code, f"https://quote.eastmoney.com/us/{code}.html"))
        else:
            code = sc[2:]
            items.append((code, sc[:2].lower() + code, f"https://quote.eastmoney.com/{sc[:2].lower()}{code}.html"))
    names = {}
    try:
        names = _qt_names([t for _, t, _ in items])
        print(f"[{cfg['label']}] 腾讯行情补全名称 {len(names)}/{len(items)}")
    except Exception as e:
        print(f"[{cfg['label']}] 取名称失败, 退回显示代码: {type(e).__name__}: {e}")
    extras = {}
    if cfg.get("market_type", "") in ("", "000003", "000004") and kind in ("a", "hk", "us"):
        try:
            extras = _em_extras(rows, kind)
        except Exception as e:
            print(f"[{cfg['label']}] 取标签/新闻失败: {type(e).__name__}: {e}")
    out = []
    for (code, tq, url), x in zip(items, rows):
        nm, pct, price = (names.get(tq.lower()) or (None, None, None))
        ex = {"price": price, "pct": pct}
        ex.update(extras.get(x["sc"], {}))
        out.append({"title": f"{nm}({code})" if nm else code, "url": url, "id": code,
                    "hot": f"{pct}%" if pct is not None else None, "extra": ex})
    return out


def _em_extras(rows, kind):
    """热股的附加信息: 特征标签 (GG)、板块标签 (BK)、关联热点新闻。返回 {sc: {tags, news}}。"""
    res = {x["sc"]: {"tags": [], "news": None} for x in rows}
    if kind == "a":
        codes = ",".join(x["sc"] for x in rows)
        base = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
        for rn, kindname, lim in (("RPT_CUSTOM_INDIVIDUAL_SHARE_DIMENSIONALITY_GG", "feat", 2),
                                  ("RPT_CUSTOM_INDIVIDUAL_SHARE_DIMENSIONALITY_BK", "board", 2)):
            try:
                r = requests.get(base, params={
                    "reportName": rn, "columns": "ALL", "source": "SECURITIES", "client": "APP",
                    "pageNumber": 1, "pageSize": 2000,
                    "filter": f'(SECURITY_CODE_MK in ("{codes}"))'},
                    headers={"User-Agent": UA}, timeout=20)
                data = ((r.json().get("result") or {}).get("data")) or []
                if kindname == "feat":
                    data.sort(key=lambda d: (str(d.get("CLASSIF_CODE")), int(d.get("INDICATOR_NUM") or 0)))
                cnt = {}
                for d in data:
                    k = d.get("SECURITY_CODE_MK")
                    nm = d.get("INDICATOR_NAME") if kindname == "feat" else d.get("BOARD_NAME")
                    if k in res and nm and cnt.get(k, 0) < lim:
                        res[k]["tags"].append({"t": nm, "k": kindname})
                        cnt[k] = cnt.get(k, 0) + 1
                print(f"[东财热股] {kindname} 标签覆盖 {len(cnt)}/{len(rows)}")
            except Exception as e:
                print(f"[东财热股] {kindname} 标签失败: {type(e).__name__}: {e}")
    try:     # 关联热点新闻
        import uuid
        if kind == "hk":
            gc = ",".join("HK|" + x["sc"].split("|")[-1] for x in rows)
        elif kind == "us":
            gc = ",".join(x["sc"] for x in rows)
        else:
            gc = ",".join(x["sc"] for x in rows)
        parm = json.dumps({"deviceid": uuid.uuid4().hex.upper(), "version": "180",
                           "product": "EastMoney", "plat": "Android", "gubaCode": gc})
        r = requests.post("https://emstockdiag.eastmoney.com/apistock/Tran/GetData",
                          json={"path": "newtopic/api/Topic/GubaCodeHotTopicNewRead", "parm": parm},
                          headers={"User-Agent": UA}, timeout=20)
        re_ = (json.loads(r.json().get("RData") or "{}").get("re")) or {}
        n = 0
        for k, lst in re_.items():
            if k in res and lst:
                res[k]["news"] = {"title": lst[0].get("name"), "summary": lst[0].get("summary"),
                                  "url": EM_SEARCH + quote(str(lst[0].get("name", "")))}
                n += 1
        print(f"[东财热股] 关联热点 {n} 只")
    except Exception as e:
        print(f"[东财热股] 关联热点失败: {type(e).__name__}: {e}")
    return res


def fetch_em_board(cfg):
    """东方财富行业/概念板块: 热度榜 + 涨跌幅/涨停家数/领涨股 (push2) + 关联热点新闻。"""
    import uuid
    r = requests.get("https://datacenter.eastmoney.com/securities/api/data/v1/get", params={
        "reportName": "RPT_BOARD_BOARDHOTSCORE", "columns": "ALL", "source": "SECURITIES",
        "client": "APP", "pageNumber": 1, "pageSize": 100, "sortColumns": "HOT_RANK",
        "sortTypes": 1, "filter": f'(BOARD_TYPE_NEW="{cfg["board_type"]}")'},
        headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    data = ((r.json().get("result") or {}).get("data")) or []
    codes = [d["NEW_BOARD_CODE"] for d in data]
    quotes = {}
    secids = ",".join("90." + c for c in codes)
    for host in ("push2.eastmoney.com", "push2delay.eastmoney.com", "82.push2.eastmoney.com"):
        try:
            qr = requests.get(f"https://{host}/api/qt/ulist.np/get", params={
                "fltt": 2, "invt": 2, "fields": "f3,f12,f14,f128,f136,f437",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281", "secids": secids},
                headers={"User-Agent": UA}, timeout=15)
            for d in ((qr.json().get("data") or {}).get("diff")) or []:
                quotes[d["f12"]] = d
            print(f"[{cfg['label']}] 行情 {host}: {len(quotes)}/{len(codes)}")
            if quotes:
                break
        except Exception as e:
            print(f"[{cfg['label']}] 行情 {host} 失败: {type(e).__name__}: {e}")
    news = {}
    try:
        parm = json.dumps({"deviceid": uuid.uuid4().hex.upper(), "version": "180",
                           "product": "EastMoney", "plat": "Android", "gubaCode": ",".join(codes)})
        nr = requests.post("https://emstockdiag.eastmoney.com/apistock/Tran/GetData",
                           json={"path": "newtopic/api/Topic/GubaCodeHotTopicNewRead", "parm": parm},
                           headers={"User-Agent": UA}, timeout=20)
        for k, lst in ((json.loads(nr.json().get("RData") or "{}").get("re")) or {}).items():
            if lst:
                news[k] = {"title": lst[0].get("name"), "summary": lst[0].get("summary"),
                           "url": EM_SEARCH + quote(str(lst[0].get("name", "")))}
        print(f"[{cfg['label']}] 关联热点 {len(news)} 个")
    except Exception as e:
        print(f"[{cfg['label']}] 关联热点失败: {type(e).__name__}: {e}")
    out = []
    for d in data:
        c = d["NEW_BOARD_CODE"]
        q = quotes.get(c) or {}
        ex = {"pct": q.get("f3"), "limitup": q.get("f437"),
              "leader": ({"name": q.get("f128"), "pct": q.get("f136")} if q.get("f128") else None),
              "news": news.get(c)}
        out.append({"title": d["BOARD_NAME"], "id": c,
                    "url": f"https://quote.eastmoney.com/bk/90.{c}.html",
                    "hot": d.get("HOT_SCORE"), "extra": ex})
    return out


THS_BASE = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/"
THS_SEARCH = "https://search.10jqka.com.cn/unifiedwap/result?w="


def fetch_ths(cfg):
    """同花顺热榜 (热股/板块/话题/可转债/期货), 统一成 title/url/id/hot/extra。"""
    kind = cfg["ths_kind"]
    r = None
    for attempt in range(3):
        try:
            r = requests.get(cfg["ths_url"], headers={"User-Agent": UA, "Accept": "*/*"}, timeout=12)
            r.raise_for_status()
            break
        except requests.exceptions.RequestException as e:
            print(f"[{cfg['label']}] 第 {attempt + 1} 次请求失败: {type(e).__name__}")
            if attempt == 2:
                raise
            time.sleep(1.5)
    j = r.json()
    if j.get("status_code") not in (0, None):
        raise RuntimeError(f"同花顺返回 status_code={j.get('status_code')} {j.get('status_msg')}")
    d = j.get("data") or {}
    out = []
    if kind == "stock":
        for x in d.get("stock_list") or []:
            code, mk = x.get("code"), cfg.get("ths_market", "a")
            url = (f"https://stockpage.10jqka.com.cn/{code}/" if mk == "a"
                   else f"https://quote.eastmoney.com/{'hk' if mk == 'hk' else 'us'}/{code}.html")
            tag = x.get("tag") or {}
            tags = [{"t": tag["popularity_tag"], "k": "feat"}] if isinstance(tag, dict) and tag.get("popularity_tag") else []
            tags += [{"t": c, "k": "board"} for c in ((tag.get("concept_tag") or [])[:2] if isinstance(tag, dict) else [])]
            ex = {"pct": x.get("rise_and_fall"), "tags": tags}
            if x.get("analyse") or x.get("analyse_title"):
                ex["news"] = {"label": "原因", "title": x.get("analyse_title"),
                              "summary": (x.get("analyse") or "").replace("\n", " ")[:140]}
            out.append({"title": x.get("name"), "url": url, "id": code, "hot": x.get("rate"), "extra": ex})
    elif kind == "plate":
        seg = "gn" if cfg.get("plate_type") == "concept" else "thshy"
        for x in d.get("plate_list") or []:
            code = x.get("code")
            tags = [{"t": x[k], "k": kk} for k, kk in (("tag", "feat"), ("hot_tag", "board")) if x.get(k)]
            note = None
            if x.get("etf_name"):
                ep = x.get("etf_rise_and_fall")
                note = f"关联ETF {x['etf_name']}" + (f" {ep:+.2f}%" if isinstance(ep, (int, float)) else "")
            out.append({"title": x.get("name"), "id": code, "hot": x.get("rate"),
                        "url": f"https://q.10jqka.com.cn/{seg}/detail/code/{code}/",
                        "extra": {"pct": x.get("rise_and_fall"), "tags": tags, "note": note}})
    elif kind == "topic":
        for x in d.get("topic_list") or []:
            u = x.get("jump_url") or ""
            if not u.startswith("http"):
                u = "https://t.10jqka.com.cn/lgt/main/frontend-main-service/topic/index.html?code=" + str(x.get("code"))
            out.append({"title": x.get("title"), "id": x.get("code"), "hot": x.get("hot_value"), "url": u,
                        "extra": {"news": {"label": "话题", "title": None, "summary": (x.get("description") or "")[:140]}}})
    elif kind == "bond":
        for x in (d if isinstance(d, list) else []):
            out.append({"title": x.get("name"), "id": x.get("code"), "hot": x.get("hot"),
                        "url": f"https://stockpage.10jqka.com.cn/{x.get('code')}/",
                        "extra": {"pct": x.get("rise_and_fall")}})
    elif kind == "future":
        for x in d.get("futures_list") or []:
            f = x.get("funds")
            note = f"资金 {f / 1e8:+.2f} 亿" if isinstance(f, (int, float)) else None
            out.append({"title": x.get("name"), "id": x.get("code"), "hot": x.get("rate"),
                        "url": THS_SEARCH + quote(str(x.get("name"))),
                        "extra": {"pct": x.get("rise_and_fall"), "note": note}})
    print(f"[{cfg['label']}] 解析 {len(out)} 条")
    return out


EM_SEARCH = "https://so.eastmoney.com/web/s?keyword="


def fetch_em_topic(cfg):
    """东方财富热门话题 (股吧话题, JSONP 接口去掉 callback 即返回 JSON)。"""
    u = ("https://gbapi.eastmoney.com/newctopic/topic/HomeTopicRead?deviceid=web&version=100"
         "&product=EastMoney&plat=Web&p=1&ps=30&needTop=true&needSubscribed=false")
    r = requests.get(u, headers={"User-Agent": UA}, timeout=20)
    txt = r.text.strip()
    m = re.match(r"^[\w$.]+\((.*)\)\s*;?$", txt, re.S)
    j = json.loads(m.group(1) if m else txt)
    lst = [t for t in (j.get("re") or []) if "有奖" not in str(t.get("name", ""))]   # 去掉平台推广/活动话题
    for t in lst:
        t["_url"] = EM_SEARCH + quote(str(t.get("name", "")))
    return {"re": lst}


def fetch_em_theme(cfg):
    """东方财富题材热榜: getHotBD 给排名与新闻, getThemeQuote 给涨跌幅/涨停家数/领涨股。"""
    import uuid

    def call(path, args):
        body = {"args": args, "client": "android", "randomCode": uuid.uuid4().hex,
                "timestamp": int(time.time() * 1000), "clientType": "cfw",
                "clientVersion": "null", "appKey": "h5-dcrb"}
        r = requests.post("https://emcfgdata.eastmoney.com/api/themeInvest/" + path,
                          json=body, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        return r.json()

    j = call("getHotBD", {"bdType": 1})
    data = j.get("data") or []
    quotes = {}
    try:
        qj = call("getThemeQuote", {"themeCodes": ",".join(t["themeCode"] for t in data)})
        for q in qj.get("data") or []:
            quotes[q.get("themeCode")] = q
        print(f"[{cfg['label']}] 题材行情 {len(quotes)}/{len(data)}")
    except Exception as e:
        print(f"[{cfg['label']}] 题材行情失败: {type(e).__name__}: {e}")
    out = []
    for t in data:
        q = quotes.get(t["themeCode"]) or {}
        news = None
        if t.get("newsTitle") or t.get("newsSummary"):
            news = {"title": t.get("newsTitle"), "summary": t.get("newsSummary"),
                    "url": EM_SEARCH + quote(str(t.get("newsTitle") or t["themeName"]))}
        out.append({"title": t["themeName"], "id": t["themeCode"], "hot": t.get("hotRankScore"),
                    "url": EM_SEARCH + quote(str(t["themeName"])),
                    "extra": {"pct": q.get("bf3"), "limitup": q.get("fex3"),
                              "leader": ({"name": q.get("securityName"), "pct": q.get("f3")} if q.get("securityName") else None),
                              "news": news}})
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
                for w in lst:
                    w["_url"] = "https://www.douyin.com/search/" + quote(str(w.get("word", "")))
                return {"word_list": lst}
        except Exception as e:
            last = e
            print(f"[抖音] {u.split('?')[0]} 失败: {type(e).__name__}: {e}")
    if last:
        raise last
    return {}

FETCHERS = {"ths": fetch_ths, "em_board": fetch_em_board, "douyin": fetch_douyin, "em_topic": fetch_em_topic, "em_theme": fetch_em_theme, "em_rank": fetch_em_rank, "xq": fetch_xq}

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
        "label": "百度热榜·财经榜",
        "site": "百度热榜", "sub": "财经榜",
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
        "fields": {"title": "word", "url": "_url", "id": "word", "hot": "hot_value"},
        "ranked": True,
    },
    "em_rank": {
        "label": "东方财富·A股热股", "site": "东方财富", "sub": "热股", "sub2": "A股",
        "enabled": True, "fetcher": "em_rank", "kind": "a",
        "list_path": "", "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot", "extra": "extra"},
        "ranked": True,
    },
    "em_rank_hk": {
        "label": "东方财富·港股热股", "site": "东方财富", "sub": "热股", "sub2": "港股",
        "enabled": True, "fetcher": "em_rank", "kind": "hk", "market_type": "000003",
        "em_url": "https://emappdata.eastmoney.com/stockrank/getAllCurrHkUsList",
        "list_path": "", "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot", "extra": "extra"},
        "ranked": True,
    },
    "em_rank_us": {
        "label": "东方财富·美股热股", "site": "东方财富", "sub": "热股", "sub2": "美股",
        "enabled": True, "fetcher": "em_rank", "kind": "us", "market_type": "000004",
        "em_url": "https://emappdata.eastmoney.com/stockrank/getAllCurrHkUsList",
        "list_path": "", "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot", "extra": "extra"},
        "ranked": True,
    },
    "em_etf": {
        "label": "东方财富·ETF人气榜", "site": "东方财富", "sub": "ETF",
        "enabled": True, "fetcher": "em_rank", "kind": "a", "market_type": "etf",
        "em_url": "https://emappdata.eastmoney.com/fundrank/getAllCurrentETFList",
        "list_path": "", "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot", "extra": "extra"},
        "ranked": True,
    },
    "em_board_industry": {
        "label": "东方财富·行业板块", "site": "东方财富", "sub": "热门板块", "sub2": "行业",
        "enabled": True, "fetcher": "em_board", "board_type": "2",
        "list_path": "", "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot", "extra": "extra"},
        "ranked": True,
    },
    "em_board_concept": {
        "label": "东方财富·概念板块", "site": "东方财富", "sub": "热门板块", "sub2": "概念",
        "enabled": True, "fetcher": "em_board", "board_type": "3",
        "list_path": "", "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot", "extra": "extra"},
        "ranked": True,
    },
    "em_theme": {
        "label": "东方财富·题材", "site": "东方财富", "sub": "热门板块", "sub2": "题材",
        "enabled": True, "fetcher": "em_theme",
        "list_path": "",
        "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot", "extra": "extra"},
        "ranked": True,
    },
    "em_topic": {
        "label": "东方财富·热门话题", "site": "东方财富", "sub": "热门话题",
        "enabled": True, "fetcher": "em_topic",
        "list_path": "re",
        "fields": {"title": "name", "url": "_url", "id": "topicid", "hot": "clickCount"},
        "ranked": True,
    },
    "ths_a_hour": {
        "label": "同花顺·热股·1小时", "site": "同花顺", "sub": "热股", "sub2": "1小时",
        "enabled": True, "fetcher": "ths", "ths_kind": "stock",
        "ths_url": THS_BASE + "stock?stock_type=a&type=hour&list_type=normal",
        "list_path": "", "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot", "extra": "extra"},
        "ranked": True,
    },
    "ths_a_day": {
        "label": "同花顺·热股·24小时", "site": "同花顺", "sub": "热股", "sub2": "24小时",
        "enabled": True, "fetcher": "ths", "ths_kind": "stock",
        "ths_url": THS_BASE + "stock?stock_type=a&type=day&list_type=normal",
        "list_path": "", "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot", "extra": "extra"},
        "ranked": True,
    },
    "ths_a_sky": {
        "label": "同花顺·热股·飙升", "site": "同花顺", "sub": "热股", "sub2": "飙升",
        "enabled": True, "fetcher": "ths", "ths_kind": "stock",
        "ths_url": THS_BASE + "stock?stock_type=a&type=hour&list_type=skyrocket",
        "list_path": "", "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot", "extra": "extra"},
        "ranked": True,
    },
    "ths_hk": {
        "label": "同花顺·热股·港股", "site": "同花顺", "sub": "热股", "sub2": "港股",
        "enabled": True, "fetcher": "ths", "ths_kind": "stock", "ths_market": "hk",
        "ths_url": THS_BASE + "stock?stock_type=hk&type=day&list_type=normal",
        "list_path": "", "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot", "extra": "extra"},
        "ranked": True,
    },
    "ths_us": {
        "label": "同花顺·热股·美股", "site": "同花顺", "sub": "热股", "sub2": "美股",
        "enabled": True, "fetcher": "ths", "ths_kind": "stock", "ths_market": "us",
        "ths_url": THS_BASE + "stock?stock_type=usa&type=day&list_type=normal",
        "list_path": "", "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot", "extra": "extra"},
        "ranked": True,
    },
    "ths_plate_concept": {
        "label": "同花顺·热门板块·概念", "site": "同花顺", "sub": "热门板块", "sub2": "概念",
        "enabled": True, "fetcher": "ths", "ths_kind": "plate", "plate_type": "concept",
        "ths_url": THS_BASE + "plate?type=concept",
        "list_path": "", "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot", "extra": "extra"},
        "ranked": True,
    },
    "ths_plate_industry": {
        "label": "同花顺·热门板块·行业", "site": "同花顺", "sub": "热门板块", "sub2": "行业",
        "enabled": True, "fetcher": "ths", "ths_kind": "plate", "plate_type": "industry",
        "ths_url": THS_BASE + "plate?type=industry",
        "list_path": "", "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot", "extra": "extra"},
        "ranked": True,
    },
    "ths_topic": {
        "label": "同花顺·热门话题", "site": "同花顺", "sub": "热门话题",
        "enabled": True, "fetcher": "ths", "ths_kind": "topic",
        "ths_url": THS_BASE + "topic?page=1&page_size=30",
        "list_path": "", "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot", "extra": "extra"},
        "ranked": True,
    },
    "ths_bond": {
        "label": "同花顺·可转债", "site": "同花顺", "sub": "可转债",
        "enabled": False, "fetcher": "ths", "ths_kind": "bond",   # 按需求停用
        "ths_url": THS_BASE + "bond",
        "list_path": "", "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot", "extra": "extra"},
        "ranked": True,
    },
    "ths_future": {
        "label": "同花顺·期货", "site": "同花顺", "sub": "期货",
        "enabled": False, "fetcher": "ths", "ths_kind": "future",   # 按需求停用
        "ths_url": THS_BASE + "future",
        "list_path": "", "fields": {"title": "title", "url": "url", "id": "id", "hot": "hot", "extra": "extra"},
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
SITE_ORDER = ["微博", "抖音", "今日头条", "百度热榜", "东方财富", "同花顺", "雪球", "华尔街见闻"]

SUB_ORDER = {"东方财富": ["热门话题", "热股", "热门板块", "ETF"],
             "同花顺": ["热股", "热门板块", "热门话题", "可转债", "期货"]}
SUB2_ORDER = ["A股", "1小时", "24小时", "飙升", "港股", "美股", "行业", "概念", "题材"]

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


def collect_one(conn, name, cfg, data=None):
    if data is None:
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
                "pub_time=COALESCE(?, pub_time), url=COALESCE(?, url) WHERE id=?",
                (ts, best, pub, rec.get("url"), item_id),
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
    from concurrent.futures import ThreadPoolExecutor
    conn = db()
    active = [(n, c) for n, c in SOURCES.items() if c.get("enabled")]

    def _get(item):
        n, c = item
        try:
            return n, fetch(n, c), None
        except Exception as e:
            return n, None, e

    # 网络请求并发执行 (每个源内部自带重试), 入库仍然串行
    with ThreadPoolExecutor(max_workers=6) as ex:
        fetched = {n: (d, e) for n, d, e in ex.map(_get, active)}
    for name, cfg in active:
        data, err = fetched[name]
        if err is not None:
            print(f"[{name}] 失败: {type(err).__name__}: {err}")
            continue
        try:
            collect_one(conn, name, cfg, data)
        except Exception as e:
            print(f"[{name}] 失败: {type(e).__name__}: {e}")
    conn.close()



# ---------------------------------------------------------------------------
# 大盘情绪: 指数/涨跌家数/涨停跌停/连板高度/成交额/板块资金 (东方财富公开接口, 每次导出时现取)
# ---------------------------------------------------------------------------
EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"
EM_POOL_UT = "7eea3edcaed734bea9cbfc24409ed989"


def _get_json(url, params, tries=4, timeout=15):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1)
    raise last


def fetch_market():
    mk = {"updated_at": now_str()}
    try:
        j = _get_json("https://push2.eastmoney.com/api/qt/ulist.np/get", {
            "fltt": 2, "invt": 2, "ut": EM_UT,
            "fields": "f2,f3,f4,f6,f12,f14,f104,f105,f106",
            "secids": "1.000001,0.399001,0.399006,1.000300"})
        diff = (j.get("data") or {}).get("diff") or []
        mk["indices"] = [{"name": d["f14"], "price": d["f2"], "pct": d["f3"], "chg": d["f4"]} for d in diff]
        by = {d["f12"]: d for d in diff}
        sh, sz = by.get("000001"), by.get("399001")
        if sh and sz:
            mk["up"] = (sh["f104"] or 0) + (sz["f104"] or 0)
            mk["down"] = (sh["f105"] or 0) + (sz["f105"] or 0)
            mk["flat"] = (sh["f106"] or 0) + (sz["f106"] or 0)
            mk["turnover"] = round(((sh["f6"] or 0) + (sz["f6"] or 0)) / 1e8, 0)
        mk["idx_at"] = now_str()
    except Exception as e:
        print(f"[大盘] 指数/涨跌家数失败: {type(e).__name__}: {e}")
    # 涨停池/跌停池: 非交易日回退到最近有数据的交易日
    for back in range(0, 6):
        d = (datetime.now(CST) - timedelta(days=back)).strftime("%Y%m%d")
        try:
            base = {"ut": EM_POOL_UT, "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": 300,
                    "sort": "fbt:asc", "date": d}
            z = _get_json("https://push2ex.eastmoney.com/getTopicZTPool", base)
            pool = (z.get("data") or {}).get("pool") or []
            if not pool:
                continue
            mk["zt"] = (z["data"].get("tc") or len(pool))
            mx = max(p.get("lbc") or 1 for p in pool)
            mk["lb_high"] = mx
            mk["lb_names"] = [p["n"] for p in pool if (p.get("lbc") or 1) == mx][:3]
            mk["pool_date"] = d
            base["sort"] = "fund:asc"
            dz = _get_json("https://push2ex.eastmoney.com/getTopicDTPool", base)
            mk["dt"] = (dz.get("data") or {}).get("tc")
            break
        except Exception as e:
            print(f"[大盘] 涨跌停池 {d} 失败: {type(e).__name__}: {e}")
    try:
        for key, po in (("flow_in", 1), ("flow_out", 0)):
            j = _get_json("https://push2.eastmoney.com/api/qt/clist/get", {
                "pn": 1, "pz": 3, "po": po, "np": 1, "fltt": 2, "invt": 2, "fid": "f62",
                "fs": "m:90+t:2", "fields": "f12,f14,f3,f62", "ut": EM_UT})
            diff = (j.get("data") or {}).get("diff") or []
            mk[key] = [{"name": d["f14"], "amt": round((d["f62"] or 0) / 1e8, 1), "pct": d["f3"]} for d in diff]
            mk["flow_at"] = now_str()
    except Exception as e:
        print(f"[大盘] 板块资金失败: {type(e).__name__}: {e}")
    print(f"[大盘] 取到字段: {', '.join(k for k in mk if k != 'updated_at')}")
    return mk


# ---------------------------------------------------------------------------
# 全网热点: 把各平台的话题类榜单按标题相似度合并, 并关联板块/个股
# ---------------------------------------------------------------------------
MERGE_SOURCES = {          # source id -> (平台名, 权重, 是否财经原生榜)
    "weibo": ("微博", 1.0, False),
    "douyin": ("抖音", 1.0, False),
    "toutiao_all": ("今日头条", 1.0, False),
    "baidu_finance": ("百度热榜", 1.0, False),
    "wscn_hot_day": ("华尔街见闻", 0.8, True),
    "em_topic": ("东财话题", 0.8, True),
    "ths_topic": ("同花顺话题", 0.8, True),
}
# 强财经词: 命中即判为财经 (可在 finance_rules.json 里用 add 追加, block 屏蔽)
FIN_KEYWORDS = [
    "A股", "港股", "美股", "股市", "股价", "股票", "股指", "大盘", "沪指", "深成指", "创业板", "科创板", "北交所",
    "恒生", "纳指", "道指", "标普", "日经", "基金", "ETF", "债市", "债券", "国债", "美债", "债务", "收益率",
    "利率", "降息", "加息", "降准", "央行", "美联储", "通胀", "CPI", "PPI", "PMI", "GDP", "关税", "汇率",
    "人民币", "美元", "日元", "欧元", "黄金", "白银", "原油", "油价", "成品油", "铜价", "碳酸锂", "稀土", "期货",
    "涨停", "跌停", "财政", "发改委", "证监会", "金融监管", "国资委", "IPO", "上市公司", "并购", "重组", "财报",
    "业绩", "净利", "营收", "市值", "估值", "融资", "减持", "增持", "回购", "分红", "退市", "做空", "牛市", "熊市",
    "泡沫", "银行", "券商", "保险", "房地产", "楼市", "房价", "贷款", "贴息", "公积金", "理财", "出口", "退税",
    "增值税", "养老金", "个税", "芯片", "半导体", "光模块", "算力", "大模型", "新能源", "光伏", "锂电", "储能",
    "存款", "信贷", "社融", "外汇", "外储", "加密货币", "比特币", "稳定币", "私募", "公募", "险资", "北向资金",
    "主力资金", "资金流", "两融", "转债", "科技股", "白酒股", "题材股", "概念股", "金融委", "财政部", "商务部",
]
# 弱相关词: 只是公司/品牌/泛消费词, 单独命中不算财经重点
WEAK_KEYWORDS = ["苹果", "特斯拉", "华为", "小米", "英伟达", "OpenAI", "消费", "上市", "进口", "机器人", "新车"]
RULES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finance_rules.json")


def load_rules():
    try:
        with open(RULES_FILE, encoding="utf-8") as f:
            r = json.load(f)
        return [str(x) for x in r.get("add", []) if x], [str(x) for x in r.get("block", []) if x]
    except Exception:
        return [], []


def fetch_listed_names():
    """从东方财富取全部 A股/港股/美股公司简称, 用来识别标题里出现的上市公司。失败时返回空集合, 不影响主流程。"""
    names = set()
    for label, fs in (("A股", "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"),
                      ("港股", "m:116+t:3,m:116+t:4,m:116+t:1,m:116+t:2"),
                      ("美股", "m:105,m:106,m:107")):
        try:
            j = _get_json("https://push2.eastmoney.com/api/qt/clist/get", {
                "pn": 1, "pz": 6000, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f3",
                "fs": fs, "fields": "f14", "ut": EM_UT}, tries=2)
            diff = (j.get("data") or {}).get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            got = 0
            for d in diff:
                n = re.sub(r"^(\*?ST|XD|XR|DR|N|C)", "", str(d.get("f14", "")).strip())
                n = re.sub(r"(-[A-Z]+|\s.*)$", "", n)
                if len(n) >= 3:
                    names.add(n)
                    got += 1
            print(f"[合并榜] {label} 公司简称 {got} 个")
        except Exception as e:
            print(f"[合并榜] {label} 公司简称获取失败: {type(e).__name__}: {e}")
    return names


def _bigrams(t):
    t = re.sub(r"[\s\W_]+", "", str(t), flags=re.UNICODE).lower()
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else {t}


def _sim(a, b):
    inter = len(a & b)
    if inter < 3:
        return 0.0
    dice = 2 * inter / (len(a) + len(b))
    contain = inter / min(len(a), len(b))
    ok = contain >= 0.7 or (contain >= 0.6 and inter >= 4)
    return max(dice, contain * 0.85 if ok else 0.0)


FIN_LIMIT, OTHER_LIMIT = 30, 10


def build_merged(sources):
    by_id = {s["id"]: s for s in sources}
    now = datetime.now(CST)
    add_kw, block_kw = load_rules()
    market_kw = FIN_KEYWORDS + add_kw
    # 财经词表: 板块/题材/个股名称 + 全部上市公司简称
    vocab = {}
    for sid in ("em_board_industry", "em_board_concept", "em_theme", "ths_plate_concept", "ths_plate_industry"):
        for it in (by_id.get(sid) or {}).get("items", []):
            t = (it["title"] or "").strip()
            if len(t) >= 3:
                vocab[t] = "board"
    for sid in ("em_rank", "ths_a_hour", "ths_a_day"):
        for it in (by_id.get(sid) or {}).get("items", []):
            t = re.sub(r"\(.*?\)$", "", it["title"] or "").strip()
            if len(t) >= 3 and not t.startswith("N"):
                vocab.setdefault(t, "stock")
    try:
        for n in fetch_listed_names():
            vocab.setdefault(n, "stock")
    except Exception as e:
        print(f"[合并榜] 上市公司名单失败: {type(e).__name__}: {e}")
    vocab_sorted = sorted(vocab.items(), key=lambda kv: -len(kv[0]))
    fin_titles = {it["title"] for it in (by_id.get("toutiao") or {}).get("items", [])}

    cands = []
    for sid, (plat, w, native) in MERGE_SOURCES.items():
        src = by_id.get(sid)
        if not src:
            continue
        n_items = max(len(src["items"]), 1)
        for it in src["items"]:
            if not it.get("title"):
                continue
            cands.append({"title": it["title"].strip(), "bg": _bigrams(it["title"]), "site": plat, "sid": sid,
                          "rank": it.get("rank"), "url": it.get("url"), "first": it.get("first_seen"),
                          "w": w, "len": n_items, "native": native or it["title"] in fin_titles})
    cands.sort(key=lambda c: (c["rank"] or 999))
    clusters = []
    for c in cands:
        best, best_s = None, 0.0
        for cl in clusters:
            if any(m["sid"] == c["sid"] for m in cl["members"]):
                continue                      # 同一榜单内的条目不合并
            sc = max(_sim(c["bg"], m["bg"]) for m in cl["members"])
            if sc > best_s:
                best, best_s = cl, sc
        if best is not None and best_s >= 0.5:
            best["members"].append(c)
        else:
            clusters.append({"members": [c]})
    out = []
    for cl in clusters:
        ms = cl["members"]
        ms.sort(key=lambda m: (m["rank"] or 999))
        head = ms[0]
        title = head["title"]                  # 代表标题: 各平台中排名最靠前的那条
        # 名次按各自榜单长度折算成 0-1 (榜首 1, 榜尾接近 0), 再乘平台权重
        score = sum(m["w"] * (1 - ((m["rank"] or m["len"]) - 1) / m["len"]) for m in ms)
        firsts = [m["first"] for m in ms if m["first"]]
        first = min(firsts) if firsts else None
        text = " ".join(m["title"] for m in ms)
        rel = []
        for term, kind in vocab_sorted:
            if term in text and not any(term in r["t"] or r["t"] in term for r in rel):
                rel.append({"t": term, "k": kind})
            if len(rel) >= 3:
                break
        blocked = any(k in text for k in block_kw)
        strong = any(m["native"] for m in ms) or bool(rel) or any(k in text for k in market_kw)
        finance = strong and not blocked
        weak = (not finance) and not blocked and any(k in text for k in WEAK_KEYWORDS)
        is_new = False
        if first:
            try:
                is_new = (now - datetime.strptime(first, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)).total_seconds() < 3600 * 2
            except ValueError:
                pass
        out.append({"title": title, "n": len(ms), "best": min((m["rank"] or 999) for m in ms),
                    "score": round(score, 3), "finance": finance, "weak": weak, "rel": rel, "first": first,
                    "new": is_new,
                    "members": [{"site": m["site"], "rank": m["rank"], "title": m["title"], "url": m["url"]} for m in ms]})
    # 两段: 财经重点按命中平台数排 (同数再按分数), 其他跨平台热点只收非财经且至少 2 个平台上榜的话题
    fin = sorted([x for x in out if x["finance"]], key=lambda x: (-x["n"], -x["score"]))[:FIN_LIMIT]
    oth = sorted([x for x in out if not x["finance"] and x["n"] >= 2], key=lambda x: (-x["n"], -x["score"]))[:OTHER_LIMIT]
    for x in fin:
        x["sec"] = "fin"
    for x in oth:
        x["sec"] = "oth"
    print(f"[合并榜] 候选 {len(cands)} 条 -> {len(out)} 个话题, 财经重点 {len(fin)} 个 (其中多平台 {sum(1 for x in fin if x['n'] > 1)}), 其他跨平台 {len(oth)} 个")
    return fin + oth


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
            "SELECT s.rank, s.hot, i.title, i.url, i.first_seen, i.seen_count, i.pub_time, s.raw "
            "FROM snapshots s JOIN items i ON i.source=s.source AND i.fingerprint=s.fingerprint "
            "WHERE s.source=? AND s.captured_at=? "
            "ORDER BY COALESCE(s.rank, 9999), s.id", (name, last)).fetchall()
        out["sources"].append({
            "id": name,
            "label": cfg.get("label", name),
            "site": cfg.get("site", cfg.get("label", name)),
            "sub": cfg.get("sub", ""),
            "sub2": cfg.get("sub2", ""),
            "ranked": bool(cfg.get("ranked")),
            "captured_at": last,
            "rank_date": (cur.execute("SELECT note FROM meta WHERE source=?", (name,)).fetchone() or [None])[0],
            "items": [{"rank": r[0], "hot": r[1], "title": r[2], "url": r[3],
                       "first_seen": r[4], "new": r[5] == 1, "pub": r[6],
                       "extra": (json.loads(r[7]).get("extra") if r[7] else None)} for r in rows],
        })
    conn.close()
    def _key(x):
        site = x.get("site")
        si = SITE_ORDER.index(site) if site in SITE_ORDER else len(SITE_ORDER)
        sub = SUB_ORDER.get(site, [])
        sb = sub.index(x.get("sub")) if x.get("sub") in sub else 0
        s2 = SUB2_ORDER.index(x.get("sub2")) if x.get("sub2") in SUB2_ORDER else 0
        return (si, sb, s2)
    out["sources"].sort(key=_key)   # 稳定排序, 同一网站内保持原来的先后
    try:
        out["merged"] = build_merged(out["sources"])
    except Exception as e:
        print(f"[合并榜] 失败: {type(e).__name__}: {e}")
        out["merged"] = []
    try:
        out["market"] = fetch_market()
    except Exception as e:
        print(f"[大盘] 失败: {type(e).__name__}: {e}")
        out["market"] = {}
    return out


def upload_site():
    bucket = get_bucket()
    data = export_latest()
    # 大盘接口偶尔取不到时, 沿用上一次成功的数据, 并保留它自己的采集时间, 前端会如实标注
    try:
        prev = json.loads(bucket.get_object(SITE_PREFIX + "latest.json").read()).get("market") or {}
        mk = data.setdefault("market", {})
        if not mk.get("indices") and prev.get("indices"):
            for k in ("indices", "up", "down", "flat", "turnover", "idx_at"):
                if k in prev:
                    mk[k] = prev[k]
            print("[大盘] 指数取数失败, 沿用上一次的数据")
        if not mk.get("flow_in") and prev.get("flow_in"):
            for k in ("flow_in", "flow_out", "flow_at"):
                if k in prev:
                    mk[k] = prev[k]
    except Exception as e:
        print(f"[大盘] 读取上一次数据失败: {type(e).__name__}: {e}")
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
