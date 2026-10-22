#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 情报日报 / AI Daily Intelligence
------------------------------------
一次运行 = 抓取全网 AI 社区榜单 + 官方动态 + 模型评测 → 生成自包含 HTML 面板。

纯标准库实现（兼容 macOS 自带 /usr/bin/python3），无需 pip 安装。

用法:
    python3 ai_daily.py            生成最新面板
    python3 ai_daily.py --open     生成并打开浏览器（每天只自动开一次）
    python3 ai_daily.py --quiet    静默（供 launchd 调用）

输出:
    output/index.html        最新一期
    output/YYYY-MM-DD.html   当日存档
    data/YYYY-MM-DD.json     当日结构化数据
    logs/run.log             运行日志
"""

import gzip
import html
import json
import re
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CONF = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
OUT, DATA, LOGS, STATE = BASE / "output", BASE / "data", BASE / "logs", BASE / "state"
for _d in (OUT, DATA, LOGS, STATE):
    _d.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
UA_REDDIT = "macos:ai-daily:v1.0 (personal dashboard; by /u/ai_daily_local)"
KW = [k.lower() for k in CONF["keywords_ai"]]
NOW = datetime.now().astimezone()
TODAY = NOW.strftime("%Y-%m-%d")
SHEET = CONF["sheet"]

log_lines = []


def _lan_ip():
    """优先取 Wi-Fi/以太网口的真实局域网 IP；UDP 法在有 TUN 网卡时会拿到假地址。"""
    import subprocess
    for iface in ("en0", "en1", "en2"):
        try:
            out = subprocess.run(["ipconfig", "getifaddr", iface],
                                 capture_output=True, text=True, timeout=3)
            ip = out.stdout.strip()
            if ip and not ip.startswith("198.18.") and not ip.startswith("127."):
                return ip
        except Exception:  # noqa: BLE001
            pass
    import socket
    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.connect(("1.1.1.1", 80))
        _ip = _s.getsockname()[0]
        _s.close()
        return _ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


LAN_IP = _lan_ip()


def log(msg):
    log_lines.append("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg))


# ---------------------------------------------------------------------------
# 抓取与文本工具
# ---------------------------------------------------------------------------
def fetch(url, timeout=25, ua=None, tries=1):
    err = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": ua or UA,
                "Accept": "application/rss+xml, application/atom+xml, application/json, "
                          "text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw, None
        except Exception as e:  # noqa: BLE001
            err = "%s: %s" % (type(e).__name__, e)
            if attempt + 1 < tries:
                time.sleep(2)
    return None, err


TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(s):
    if not s:
        return ""
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def clip(s, n=200):
    s = strip_tags(s)
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def parse_dt(value):
    if not value:
        return None
    v = str(value).strip()
    try:
        dt = parsedate_to_datetime(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        pass
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def ago(dt):
    if not dt:
        return ""
    secs = (NOW - dt).total_seconds()
    if secs < 0:
        return "刚刚"
    if secs < 3600:
        return "%d 分钟前" % max(1, int(secs // 60))
    if secs < 86400:
        return "%d 小时前" % int(secs // 3600)
    return "%d 天前" % int(secs // 86400)


_ASCII_KW = [k for k in KW if re.fullmatch(r"[a-z0-9.\-+ ]+", k)]
_CJK_KW = [k for k in KW if k not in _ASCII_KW]
# 短关键词（ai / llm / rag…）必须整词匹配，否则 "mailing"、"said" 都会误判成 AI
_AI_RE = re.compile(
    "(?<![a-z0-9])(?:%s)(?:s|es)?(?![a-z0-9])"
    % "|".join(re.escape(k) for k in _ASCII_KW))


NOISE = [
    "涨停", "跌停", "杀跌", "收评", "午评", "早评", "概念股", "融资余额", "ETF",
    "股价", "市值蒸发", "减持", "增持", "龙虎榜", "创业板指数", "沪指", "深成指",
    "邀请函", "线下活动", "报名开启", "直播预告", "扫码报名", "议程公布",
]


def is_noise(text):
    t = text or ""
    return any(k in t for k in NOISE)


def is_ai(text):
    t = (text or "").lower()
    if _AI_RE.search(t):
        return True
    return any(k in t for k in _CJK_KW)


def tokens(s):
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff ]+", " ", (s or "").lower())
    return {w for w in s.split() if len(w) > 2}


def similar(a, b):
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.62


def local(tag):
    return str(tag).split("}")[-1].lower()


# ---------------------------------------------------------------------------
# RSS / Atom 解析（标准库，命名空间无关）
# ---------------------------------------------------------------------------
def parse_feed(raw, source_name, source_id, weight):
    items = []
    try:
        text = raw.decode("utf-8", "ignore")
        text = text.replace("&nbsp;", "&#160;")
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        root = ET.fromstring(text)
    except Exception as e:  # noqa: BLE001
        return items, "XML 解析失败: %s" % e

    def child_text(el, names):
        for c in el:
            if local(c.tag) in names:
                t = "".join(c.itertext())
                if t.strip():
                    return t
        return ""

    nodes = [e for e in root.iter() if local(e.tag) in ("item", "entry")]
    for it in nodes:
        title = child_text(it, ("title",))
        if not title.strip():
            continue

        link = ""
        for c in it:
            if local(c.tag) != "link":
                continue
            href = c.get("href")
            if href:
                link = href
                break
            if (c.text or "").strip():
                link = c.text.strip()
                break
        if not link:
            g = child_text(it, ("guid", "id"))
            if g.startswith("http"):
                link = g

        body = (child_text(it, ("description",)) or child_text(it, ("summary",))
                or child_text(it, ("content",)) or child_text(it, ("encoded",)))
        pub = child_text(it, ("pubdate", "published", "updated", "date"))
        real_source = source_name
        for c in it:
            if local(c.tag) == "source" and (c.text or "").strip():
                real_source = strip_tags(c.text)
                break

        clean_title = title.strip()
        if real_source and clean_title.endswith(" - " + real_source):
            clean_title = clean_title[: -(len(real_source) + 3)].strip()
        items.append({
            "title": clip(clean_title, 180),
            "url": link,
            "summary": clip(body, 240),
            "source": real_source or source_name,
            "source_id": source_id,
            "weight": weight,
            "published": parse_dt(pub),
            "kind": "feed",
        })
    return items, None


# ---------------------------------------------------------------------------
# 采集器
# ---------------------------------------------------------------------------
def collect_feeds():
    out, status = [], {}

    def one(cfg):
        raw, err = fetch(cfg["url"])
        if not raw:
            return cfg["id"], cfg["name"], [], err
        items, perr = parse_feed(raw, cfg["name"], cfg["id"], cfg["weight"])
        return cfg["id"], cfg["name"], items, perr

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(one, c): c for c in CONF["feeds"]}
        for f in as_completed(futs):
            cfg = futs[f]
            try:
                sid, name, items, err = f.result()
            except Exception as e:  # noqa: BLE001
                sid, name, items, err = cfg["id"], cfg["name"], [], str(e)
            status[sid] = {"name": name, "n": len(items), "err": err if not items else None}
            out.extend(items)
    return out, status


def collect_arxiv():
    items, status = [], {}

    def one(cfg):
        raw, err = fetch(cfg["url"], timeout=40)
        if not raw:
            return cfg["name"], [], err
        its, perr = parse_feed(raw, cfg["name"], "arxiv", 12)
        for i in its:
            i["title"] = re.sub(r"\s*\(arXiv:[\d.]+[^)]*\)\s*$", "", i["title"])
            i["summary"] = re.sub(
                r"^arXiv:\S+\s*Announce Type:\s*\w+\s*Abstract:\s*", "", i["summary"])
        return cfg["name"], its, perr

    with ThreadPoolExecutor(max_workers=2) as ex:
        for f in as_completed([ex.submit(one, c) for c in CONF["arxiv_feeds"]]):
            name, its, err = f.result()
            status[name] = {"name": name, "n": len(its), "err": err if not its else None}
            items.extend(its)
    return items, status


REDDIT_CACHE = STATE / "reddit_cache.json"


def _load_rcache():
    try:
        return json.loads(REDDIT_CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_rcache(c):
    try:
        REDDIT_CACHE.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def collect_reddit():
    """Reddit 限流严格：串行 + 长间隔 + 本地缓存兜底。"""
    out, status = [], {}
    cache = _load_rcache()
    fails = 0
    for idx, cfg in enumerate(CONF["reddit_subs"]):
        label, sub = cfg["label"], cfg["sub"]
        url = "https://www.reddit.com/r/%s/top/.rss?t=day&limit=12" % sub
        its, err = [], None
        if fails >= 2:
            err = "跳过（Reddit 限流）"
        else:
            for attempt in range(2):
                raw, err = fetch(url, timeout=20, ua=UA_REDDIT)
                if raw:
                    its, perr = parse_feed(raw, label, "reddit_" + sub, cfg["weight"])
                    err = perr
                    break
                if "429" in (err or ""):
                    fails += 1
                    time.sleep(6 * (attempt + 1))
                else:
                    time.sleep(2)

        if its:
            cache[sub] = {"fetched": NOW.isoformat(), "label": label,
                          "items": [_plain(i) for i in its]}
        elif sub in cache:
            for i in cache[sub]["items"]:
                i2 = dict(i)
                i2["published"] = parse_dt(i2.get("published"))
                i2["stale"] = True
                its.append(i2)
            err = "%s（用 %s 缓存）" % (err, cache[sub]["fetched"][:16])

        for rank, i in enumerate(its):
            i["rank"] = rank
            i["kind"] = "reddit"
            i["sub"] = label
        status["reddit_" + sub] = {"name": label, "n": len(its),
                                   "err": None if its else err}
        out.extend(its)
        if idx < len(CONF["reddit_subs"]) - 1 and fails < 2:
            time.sleep(8.0)
    _save_rcache(cache)
    return out, status


def collect_hn():
    status, hits = {}, []
    for key in ("hn_front", "hn_query"):
        url = CONF["urls"][key]
        if "{ts}" in url:
            url = url.replace("{ts}", str(int((NOW - timedelta(hours=36)).timestamp())))
        raw, err = fetch(url)
        status[key] = {"name": "HN " + key, "n": 0, "err": err}
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            status[key]["err"] = "JSON 解析失败: %s" % e
            continue
        for h in data.get("hits", []):
            title = h.get("title") or ""
            if not title:
                continue
            hits.append({
                "title": clip(title, 180),
                "url": h.get("url") or ("https://news.ycombinator.com/item?id=%s"
                                        % h.get("objectID")),
                "hn_url": "https://news.ycombinator.com/item?id=%s" % h.get("objectID"),
                "points": h.get("points") or 0,
                "comments": h.get("num_comments") or 0,
                "source": "Hacker News",
                "source_id": "hn",
                "weight": 32,
                "published": parse_dt(h.get("created_at")),
                "kind": "hn",
            })
        status[key]["n"] = len(data.get("hits", []))
        status[key]["err"] = None

    seen, uniq = set(), []
    for h in sorted(hits, key=lambda x: -x["points"]):
        if h["url"] in seen:
            continue
        seen.add(h["url"])
        uniq.append(h)
    return [h for h in uniq if is_ai(h["title"])], status


GH_ART = re.compile(r"<article[^>]*Box-row[^>]*>(.*?)</article>", re.S)


def collect_github():
    raw, err = fetch(CONF["urls"]["github_trending"], timeout=30)
    status = {"github": {"name": "GitHub Trending", "n": 0, "err": err}}
    if not raw:
        return [], status
    page = raw.decode("utf-8", "ignore")
    repos = []
    for art in GH_ART.findall(page):
        m = re.search(r"<h2[^>]*>\s*<a[^>]*href=\"/([^\"]+)\"", art)
        if not m:
            continue
        full = m.group(1).strip("/")
        d = re.search(r"<p class=\"col-9[^\"]*\">(.*?)</p>", art, re.S)
        lang = re.search(r"itemprop=\"programmingLanguage\"[^>]*>([^<]+)<", art)
        st = re.search(r"([\d,]+)\s*stars today", art)
        total = re.search(r"href=\"/[^\"]+/stargazers\"[^>]*>.*?</svg>\s*([\d,]+)", art, re.S)
        repos.append({
            "repo": full,
            "url": "https://github.com/" + full,
            "desc": clip(d.group(1), 180) if d else "",
            "lang": lang.group(1).strip() if lang else "",
            "stars_today": int(st.group(1).replace(",", "")) if st else 0,
            "stars_total": total.group(1) if total else "",
        })
    ai_repos = [r for r in repos if is_ai(r["repo"] + " " + r["desc"])]
    status["github"] = {"name": "GitHub Trending", "n": len(ai_repos), "err": None}
    return ai_repos, status


def collect_hf():
    raw, err = fetch(CONF["urls"]["hf_models"], timeout=30)
    status = {"hf": {"name": "Hugging Face", "n": 0, "err": err}}
    if not raw:
        return [], status
    try:
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        status["hf"]["err"] = "JSON 解析失败: %s" % e
        return [], status
    out = []
    for m in data:
        mid = m.get("id", "")
        if "gguf" in mid.lower():
            continue
        out.append({
            "id": mid,
            "url": "https://huggingface.co/" + mid,
            "likes": m.get("likes") or 0,
            "downloads": m.get("downloads") or 0,
            "trending": round(float(m.get("trendingScore") or 0), 1),
            "task": m.get("pipeline_tag") or "",
            "created": parse_dt(m.get("createdAt")),
            "lib": m.get("library_name") or "",
        })
    out.sort(key=lambda m: (-m["trending"], -m["likes"]))
    out = out[: SHEET["hf_count"]]
    status["hf"] = {"name": "Hugging Face", "n": len(out), "err": None if out else err}
    return out, status


def collect_producthunt():
    raw, err = fetch(CONF["urls"]["producthunt"], timeout=30)
    status = {"ph": {"name": "Product Hunt", "n": 0, "err": err}}
    if not raw:
        return [], status
    items, perr = parse_feed(raw, "Product Hunt", "ph", 18)
    items.sort(key=lambda i: (not is_ai(i["title"] + " " + i["summary"])))
    items = items[: SHEET["ph_count"]]
    status["ph"] = {"name": "Product Hunt", "n": len(items), "err": None if items else perr}
    return items, status


def collect_openrouter():
    raw, err = fetch(CONF["urls"]["openrouter"], timeout=30)
    status = {"openrouter": {"name": "OpenRouter", "n": 0, "err": err}}
    if not raw:
        return [], status
    try:
        data = json.loads(raw).get("data", [])
    except Exception as e:  # noqa: BLE001
        status["openrouter"]["err"] = "JSON 解析失败: %s" % e
        return [], status
    cutoff = NOW - timedelta(days=SHEET["or_new_days"])
    rows = []
    for m in data:
        dt = None
        if m.get("created"):
            try:
                dt = datetime.fromtimestamp(int(m["created"]), tz=timezone.utc).astimezone()
            except Exception:  # noqa: BLE001
                dt = None
        pr = m.get("pricing") or {}

        def price(k):
            try:
                v = float(pr.get(k, 0)) * 1_000_000
                return round(v, 3) if v > 0 else 0.0
            except (TypeError, ValueError):
                return 0.0

        rows.append({
            "id": m.get("id"), "name": m.get("name") or m.get("id"),
            "created": dt, "ctx": m.get("context_length") or 0,
            "pin": price("prompt"), "pout": price("completion"),
            "modality": ((m.get("architecture") or {}).get("input_modalities") or []),
        })
    new = [r for r in rows if r["created"] and r["created"] >= cutoff]
    new.sort(key=lambda r: -r["created"].timestamp())
    new = new[: SHEET["or_new_count"]]
    status["openrouter"] = {"name": "OpenRouter", "n": len(new), "err": None}
    return new, status


def collect_aa():
    """Artificial Analysis：解析 Next.js flight 内嵌的模型评测数组。"""
    raw, err = fetch(CONF["urls"]["artificial_analysis"], timeout=45)
    status = {"aa": {"name": "Artificial Analysis", "n": 0, "err": err}}
    if not raw:
        return [], status
    try:
        page = raw.decode("utf-8", "ignore")
        chunks = re.findall(r'self\.__next_f\.push\(\[1,(".*?")\]\)</script>', page, re.S)
        if not chunks:
            raise ValueError("未找到 flight 数据块")
        payload = "".join(json.loads(c) for c in chunks)
        i = payload.find('"initialData":[')
        if i < 0:
            raise ValueError("未找到 initialData")
        start = payload.find("[", i)
        depth, instr, j = 0, False, start
        while j < len(payload):
            c = payload[j]
            if instr:
                if c == "\\":
                    j += 2
                    continue
                if c == '"':
                    instr = False
                j += 1
                continue
            if c == '"':
                instr = True
            elif c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        models = json.loads(payload[start: j + 1])
    except Exception as e:  # noqa: BLE001
        status["aa"]["err"] = "解析失败: %s" % e
        return [], status

    out = []
    for m in models:
        if m.get("intelligenceIndex") is None:
            continue
        cost = (m.get("intelligenceIndexCostPerTask") or {}).get("total")
        out.append({
            "name": m.get("name"),
            "short": m.get("shortName") or m.get("name"),
            "release_date": m.get("releaseDate"),
            "idx": round(float(m["intelligenceIndex"]), 1),
            "cost": round(float(cost), 3) if isinstance(cost, (int, float)) else None,
            "speed": round(float(m.get("medianOutputSpeed") or 0)),
            "open": bool(m.get("isOpenWeights")),
            "creator": (m.get("creator") or {}).get("name", ""),
            "estimated": bool(m.get("intelligenceIndexIsEstimated")),
        })
    out.sort(key=lambda x: -x["idx"])
    status["aa"] = {"name": "Artificial Analysis", "n": len(out), "err": None if out else "无数据"}
    return out, status


# ---------------------------------------------------------------------------
# 中文全网热榜（百度 / 头条 / B站 / V2EX / 掘金）
# ---------------------------------------------------------------------------
HOT_PLATFORMS = [
    ("baidu", "百度热搜"),
    ("toutiao", "头条热榜"),
    ("bilibili", "B站热门"),
    ("v2ex", "V2EX 热门"),
    ("juejin", "掘金热榜"),
]


def collect_hot_cn():
    """各平台公开热榜接口，任一失败只影响自己那一栏。"""
    out, status = [], {}

    def baidu():
        d = json.loads(fetch("https://top.baidu.com/api/board?platform=wise&tab=realtime")[0])
        items = []
        for card in d["data"]["cards"]:
            for grp in card.get("content", []):
                for it in grp.get("content", []):
                    if it.get("word"):
                        items.append({"title": it["word"], "url": it.get("url") or "",
                                      "hot": ""})
        return items

    def toutiao():
        d = json.loads(fetch("https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc")[0])
        return [{"title": x.get("Title", ""), "url": x.get("Url") or "",
                 "hot": "{:,}".format(int(x.get("HotValue") or 0))}
                for x in d.get("data", []) if x.get("Title")]

    def bilibili():
        d = json.loads(fetch("https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all")[0])
        return [{"title": v.get("title", ""),
                 "url": "https://www.bilibili.com/video/" + (v.get("bvid") or ""),
                 "hot": "{:,}".format(int((v.get("stat") or {}).get("view") or 0))}
                for v in (d.get("data") or {}).get("list", []) if v.get("title")]

    def v2ex():
        d = json.loads(fetch("https://www.v2ex.com/api/topics/hot.json")[0])
        return [{"title": x.get("title", ""), "url": x.get("url") or "",
                 "hot": "%d 回复" % (x.get("replies") or 0)} for x in d if x.get("title")]

    def juejin():
        d = json.loads(fetch("https://api.juejin.cn/content_api/v1/content/article_rank"
                             "?category_id=1&type=hot")[0])
        return [{"title": (x.get("content") or {}).get("title", ""),
                 "url": "https://juejin.cn/post/" + str((x.get("content") or {}).get("content_id", "")),
                 "hot": "{:,} 阅读".format(int((x.get("content_counter") or {}).get("view") or 0))}
                for x in d.get("data", []) if (x.get("content") or {}).get("title")]

    fn_map = {"baidu": baidu, "toutiao": toutiao, "bilibili": bilibili,
              "v2ex": v2ex, "juejin": juejin}

    def one(pid):
        try:
            raw, err = fetch({"baidu": "https://top.baidu.com/api/board?platform=wise&tab=realtime",
                              "toutiao": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
                              "bilibili": "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all",
                              "v2ex": "https://www.v2ex.com/api/topics/hot.json",
                              "juejin": "https://api.juejin.cn/content_api/v1/content/article_rank"
                                        "?category_id=1&type=hot"}[pid], timeout=15)
            if not raw:
                return pid, [], err
            return pid, fn_map[pid](), None
        except Exception as e:  # noqa: BLE001
            return pid, [], "%s: %s" % (type(e).__name__, e)

    with ThreadPoolExecutor(max_workers=5) as ex:
        for f in as_completed([ex.submit(one, pid) for pid, _ in HOT_PLATFORMS]):
            pid, items, err = f.result()
            label = dict(HOT_PLATFORMS)[pid]
            for i, it in enumerate(items[: SHEET.get("hot_count", 8)], 1):
                it["platform"] = label
                it["rank"] = i
                out.append(it)
            status["hot_" + pid] = {"name": label, "n": len(items), "err": err}
    return out, status


# ---------------------------------------------------------------------------
# 英文标题翻译（带本地缓存，只翻译没见过的）
# ---------------------------------------------------------------------------
TR_CACHE = STATE / "translate_cache.json"
CJK = re.compile(r"[\u4e00-\u9fff]")

# 必须保留原文的词：品牌 / 产品 / 机构 / 技术缩写
_BRANDS = sorted({b for b in CONF.get("brands", []) if b}, key=len, reverse=True)
_BRAND_RE = re.compile(r"(?<![A-Za-z0-9])(?:%s)(?![A-Za-z0-9])"
                       % "|".join(re.escape(b) for b in _BRANDS), re.I) if _BRANDS else None
# 型号/版本类：Kitten-TTS、DeepSeek-V4.1、H100
_PROTECT = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z][A-Za-z0-9]*(?:[-.][A-Za-z0-9]+)+"
    r"|[A-Z]{3,}[0-9]*|[A-Za-z]+\d[A-Za-z0-9.]*)(?![A-Za-z0-9])")


def has_cjk(s):
    return bool(CJK.search(s or ""))


def _load_tr():
    try:
        return json.loads(TR_CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_tr(c):
    try:
        TR_CACHE.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _protect(text, tag):
    """不该翻的词 → [[tag-i]]。品牌与型号一次扫描、长匹配优先。"""
    cands = []
    if _BRAND_RE is not None:
        for m in _BRAND_RE.finditer(text):
            cands.append((m.start(), m.end(), m.group(0)))
    for m in _PROTECT.finditer(text):
        tok = m.group(0)
        if not (re.search(r"[A-Z]", tok[1:]) or re.search(r"\d", tok)):
            continue          # 放过 Open-source 这类普通复合词
        cands.append((m.start(), m.end(), tok))
    cands.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    chosen, last = [], -1
    for a, b, txt in cands:
        if a < last:
            continue
        chosen.append((a, b, txt))
        last = b

    out, names, pos = [], [], 0
    for a, b, txt in chosen:
        out.append(text[pos:a])
        names.append(txt)
        out.append("[[%s-%d]]" % (tag, len(names) - 1))
        pos = b
    out.append(text[pos:])
    return "".join(out), names


def _restore(zh, tag, names):
    for i, nm in enumerate(names):
        zh = zh.replace("[[%s-%d]]" % (tag, i), nm)
        zh = zh.replace("[[ %s-%d ]]" % (tag, i), nm)
    return zh


def _missing(zh, tag, count):
    return [j for j in range(count) if ("[[%s-%d]]" % (tag, j)) not in zh]


PHRASE_FIX = [
    ("二重唱", "Duet"),
    ("Google 云", "Google Cloud"),
    ("新的并发症", "新隐忧"),
    ("并发症", "隐忧"),
    ("繁荣", "热潮"),
    ("AI 特工", "AI 智能体"),
    ("特工如何", "智能体如何"),
    ("AI 繁荣", "AI 热潮"),
]


def _phrasefix(zh):
    for a, b in PHRASE_FIX:
        zh = zh.replace(a, b)
    return zh


def _spacefix(zh):
    zh = re.sub(r"([\u4e00-\u9fff])([A-Za-z0-9])", r"\1 \2", zh)
    zh = re.sub(r"([A-Za-z0-9])([\u4e00-\u9fff])", r"\1 \2", zh)
    zh = re.sub(r"\s+", " ", zh)
    zh = re.sub(r"\s+([，。：；！？、）】」])", r"\1", zh)
    zh = re.sub(r"([（【「])\s+", r"\1", zh)
    return zh.strip()


# --- 通道 1：Google（批量，质量最好；高峰期会 429） ---
def _google_batch(texts):
    q = "".join("&q=" + urllib.parse.quote(t) for t in texts)
    url = ("https://clients5.google.com/translate_a/t?client=dict-chrome-ex"
           "&sl=auto&tl=zh-CN" + q)
    raw, _ = fetch(url, timeout=20)
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or len(data) != len(texts):
        return None
    out = []
    for item in data:
        if isinstance(item, list) and item and isinstance(item[0], str):
            out.append(item[0])
        elif isinstance(item, str):
            out.append(item)
        else:
            return None
    return out


def _google_single(text):
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto"
           "&tl=zh-CN&dt=t&q=" + urllib.parse.quote(text))
    raw, _ = fetch(url, timeout=15)
    if not raw:
        return None
    try:
        d = json.loads(raw.decode("utf-8", "ignore"))
        return "".join(seg[0] for seg in d[0] if seg and seg[0])
    except Exception:  # noqa: BLE001
        return None


# --- 通道 2：MyMemory（免 key，日额度有限，够翻几十条标题） ---
def _mm_call(q):
    url = ("https://api.mymemory.translated.net/get?q=%s&langpair=en|zh-CN"
           % urllib.parse.quote(q))
    raw, _ = fetch(url, timeout=20)
    if not raw:
        return None
    try:
        d = json.loads(raw.decode("utf-8", "ignore"))
        return html.unescape(d.get("responseData", {}).get("translatedText", "") or "")
    except Exception:  # noqa: BLE001
        return None


def _mm_batch(texts):
    out = _mm_call("\n".join(texts))
    if not out:
        return None
    lines = [l.strip() for l in out.split("\n") if l.strip()]
    return lines if len(lines) == len(texts) else None


def _mm_single(text):
    return _mm_call(text)


BARRIERS = {"n": 0}   # Google 连续失败次数，失败两次就不再等它

LLM = CONF.get("translator") or {}
LLM_PROMPT = (
    "把下面每条英文标题翻译成简洁的中文标题。要求：\n"
    "1) 公司名、产品名、模型名、人名保持英文原样（如 OpenAI、Claude、DeepSeek-V4）。\n"
    "2) 只输出「序号. 译文」，一行一条，条数与输入一致，不要解释、不要合并。\n"
    "3) 不要加书名号或引号。\n\n"
)


def _llm_translate(texts):
    provider = (LLM.get("provider") or "").strip()
    key = (LLM.get("api_key") or "").strip()
    if not provider or not key:
        return None
    base = LLM.get("base_url") or {
        "deepseek": "https://api.deepseek.com/v1/chat/completions",
        "openrouter": "https://openrouter.ai/api/v1/chat/completions",
        "openai": "https://api.openai.com/v1/chat/completions",
    }.get(provider, "")
    if not base:
        return None
    model = LLM.get("model") or {"deepseek": "deepseek-chat"}.get(provider, "")
    body = "\n".join("%d. %s" % (i + 1, t) for i, t in enumerate(texts))
    payload = json.dumps({
        "model": model,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": LLM_PROMPT + body}],
    }).encode()
    try:
        req = urllib.request.Request(base, data=payload, headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        text = data["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        log("LLM 翻译失败：%s" % e)
        return None
    lines = [re.sub(r"^\s*\d+[.、)]\s*", "", l).strip()
             for l in text.splitlines() if l.strip()]
    return lines if len(lines) == len(texts) else None


def _translate_one(original, tag, names):
    """单条走尽各通道，返回 (译文 or None)"""
    prep, _ = _protect(original, tag)
    for fn in (_google_single, _mm_single):
        for _ in range(2):
            zh = fn(prep)
            if not zh:
                time.sleep(1.2)
                continue
            if _missing(zh, tag, len(names)):
                return None
            return _phrasefix(_spacefix(_restore(zh, tag, names)))
        time.sleep(0.8)
    return None


def translate_titles(titles):
    """英文标题 → 中文；品牌/型号用占位符保住；结果永久缓存。"""
    cache = _load_tr()
    uniq = [t for t in dict.fromkeys(titles) if t]
    need = [t for t in uniq if len(t) > 3 and not has_cjk(t) and t not in cache]
    if not need:
        return {t: _phrasefix(cache[t]) for t in uniq if t in cache}

    prepped, names = [], []
    for i, t in enumerate(need):
        pt, nm = _protect(t, "t%d" % i)
        prepped.append(pt)
        names.append(nm)

    results = {}
    # 先批量
    for start in range(0, len(need), 8):
        chunk = list(range(start, min(start + 8, len(need))))
        sub = [prepped[i] for i in chunk]
        got = _llm_translate([need[i] for i in chunk])
        if got is None and BARRIERS["n"] < 2:
            got = _google_batch(sub)
            if got is None:
                BARRIERS["n"] += 1
        if got is None:
            got = _mm_batch(sub)
        if got is None:
            continue
        for idx, zh in zip(chunk, got):
            if not zh:
                continue
            left = _missing(zh, "t%d" % idx, len(names[idx]))
            if left:
                continue
            results[idx] = _phrasefix(_spacefix(_restore(zh, "t%d" % idx, names[idx])))
        time.sleep(0.4)

    # 批量没覆盖的逐条补
    for idx in range(len(need)):
        if idx in results:
            continue
        zh = _translate_one(need[idx], "t%d" % idx, names[idx])
        if zh:
            results[idx] = zh

    for idx, zh in results.items():
        if zh and has_cjk(zh) and zh.strip() != need[idx].strip():
            cache[need[idx]] = zh
    _save_tr(cache)
    return {t: _phrasefix(cache[t]) for t in uniq if t in cache}


# ---------------------------------------------------------------------------
# 排序与筛选
# ---------------------------------------------------------------------------
def build_headlines(feed_items, hn_items, reddit_items):
    pool = []
    horizon = NOW - timedelta(hours=CONF["headline_hours"])

    for it in feed_items:
        if it["source_id"] == "producthunt":
            continue
        if it["published"] and it["published"] < horizon:
            continue
        if is_noise(it["title"]):
            continue
        if not is_ai(it["title"] + " " + it["summary"]):
            continue
        pool.append(it)

    for h in hn_items:
        if h["published"] and h["published"] < horizon:
            continue
        h = dict(h)
        h["summary"] = "%d 分 · %d 条评论" % (h["points"], h["comments"])
        pool.append(h)

    for r in reddit_items:
        if r["published"] and r["published"] < horizon:
            continue
        if r.get("rank", 99) > 8:
            continue
        pool.append(r)

    pool.sort(key=lambda x: -x.get("weight", 10))
    merged = []
    for it in pool:
        dup = next((m for m in merged if similar(m["title"], it["title"])), None)
        if dup:
            dup["sources"].add(it["source"])
            continue
        it = dict(it)
        it["sources"] = {it["source"]}
        merged.append(it)

    for it in merged:
        age_h = 0.0
        if it.get("published"):
            age_h = max(0.0, (NOW - it["published"]).total_seconds() / 3600)
        recency = 1.0 / (1.0 + age_h / 14.0)
        engage = 0.0
        if it["kind"] == "hn":
            engage = min(2.5, it["points"] / 220.0)
        elif it["kind"] == "reddit":
            engage = max(0.0, (9 - it.get("rank", 9)) / 6.0)
        resonance = 1.0 + 0.45 * (len(it["sources"]) - 1)
        it["score"] = round(it["weight"] * recency * (1 + engage) * resonance, 1)

    merged.sort(key=lambda x: -x["score"])
    return merged[: CONF["max_headlines"]]


def build_zh(feed_items):
    out = []
    horizon = NOW - timedelta(hours=48)
    for it in feed_items:
        if it["source_id"] not in ("qbitai", "ithome", "gnews_zh"):
            continue
        if it["published"] and it["published"] < horizon:
            continue
        if is_noise(it["title"]):
            continue
        if it["source_id"] == "ithome" and not is_ai(it["title"] + " " + it["summary"]):
            continue
        out.append(it)
    out.sort(key=lambda x: -(x["published"].timestamp() if x["published"] else 0))
    return out[: SHEET["zh_count"]]


def build_papers(papers):
    boost = ["state-of-the-art", "sota", "we introduce", "we propose", "benchmark",
             "agent", "reasoning", "multimodal", "mixture of experts", "moe",
             "reinforcement learning", "test-time", "long context", "efficient", "scaling"]
    scored = []
    for p in papers:
        t = (p["title"] + " " + p["summary"]).lower()
        bonus = sum(1 for w in boost if w in t)
        recency = 0.0
        if p["published"]:
            recency = max(0.0, 1 - (NOW - p["published"]).total_seconds() / (3 * 86400))
        scored.append((bonus + recency * 2.0, p))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[: SHEET["paper_count"]]]


def _plain(obj):
    """datetime/set → JSON 可存类型"""
    out = {}
    for k, v in obj.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, set):
            out[k] = sorted(v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# HTML 渲染
# ---------------------------------------------------------------------------
E = html.escape


def rel(url, text):
    return '<a href="%s" target="_blank" rel="noopener">%s</a>' % (E(url or "#"), E(text))


HUB_LINKS = [
    ("Artificial Analysis 榜单", "https://artificialanalysis.ai/leaderboards/models"),
    ("LMArena 盲测排名", "https://arena.ai/leaderboard"),
    ("OpenRouter 用量与价格", "https://openrouter.ai/rankings"),
    ("Hugging Face 趋势", "https://huggingface.co/models?sort=trending"),
    ("GitHub Trending", "https://github.com/trending?since=daily"),
    ("Hacker News", "https://news.ycombinator.com/"),
    ("Aider 编程榜", "https://aider.chat/docs/leaderboards/"),
    ("SWE-bench", "https://www.swebench.com/"),
]

CSS = """
:root{
  --bg:#0a0b0e; --card:#12141a; --card2:#171a21; --line:rgba(255,255,255,.07);
  --fg:#eef1f7; --dim:#8d97a8; --dim2:#6b7486;
  --acc:#6d8fff; --acc2:#3ddc97; --hot:#ff6b8a;
  --r:18px;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{overflow-x:hidden;scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);overflow-x:hidden;max-width:100vw;
  font:16px/1.62 -apple-system,BlinkMacSystemFont,"SF Pro SC","PingFang SC","Helvetica Neue",Arial,sans-serif;
  -webkit-font-smoothing:antialiased;font-feature-settings:"tnum";
  padding:0 0 calc(84px + env(safe-area-inset-bottom))}
a{color:inherit;text-decoration:none}
a:active{opacity:.65}
.wrap{max-width:1180px;margin:0 auto;padding:0 18px}

/* 头部 */
header{padding:26px 0 4px;display:flex;justify-content:space-between;align-items:flex-start;gap:14px}
h1{margin:0;font-size:27px;font-weight:750;letter-spacing:-.7px}
h1 em{font-style:normal;background:linear-gradient(100deg,#7f9dff,#3ddc97);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:var(--dim);font-size:12.5px;margin-top:6px;letter-spacing:.2px}
.stamp{text-align:right;color:var(--dim2);font-size:11.5px;line-height:1.7;white-space:nowrap}
.stamp b{color:var(--dim);font-weight:600}

/* 统计 */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0 16px}
.stat{background:var(--card);border-radius:var(--r);padding:14px 15px}
.stat .k{font-size:11px;color:var(--dim2);letter-spacing:.3px}
.stat .v{font-size:23px;font-weight:750;margin-top:3px;letter-spacing:-.6px}
.stat .v.sm{font-size:15px;line-height:1.35;padding-top:4px;font-weight:650;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
  max-height:2.7em}
.stat .d{font-size:11px;color:var(--dim2);margin-top:2px}

/* 搜索 */
input#q{width:100%;background:var(--card);border:1px solid transparent;color:var(--fg);
  border-radius:14px;padding:12px 15px;font-size:15px;outline:none;margin:0 0 22px;
  -webkit-appearance:none}
input#q::placeholder{color:var(--dim2)}
input#q:focus{border-color:rgba(109,143,255,.55);background:var(--card2)}

/* 吸顶导航 */
nav.nav{position:sticky;top:0;z-index:20;display:flex;gap:6px;overflow-x:auto;
  -webkit-overflow-scrolling:touch;scrollbar-width:none;
  background:rgba(10,11,14,.86);-webkit-backdrop-filter:saturate(180%) blur(14px);
  backdrop-filter:saturate(180%) blur(14px);border-bottom:1px solid var(--line);
  padding:9px 18px;margin:0 -18px 18px}
nav.nav::-webkit-scrollbar{display:none}
nav.nav a{font-size:13px;padding:5px 12px;border-radius:20px;color:var(--dim);
  white-space:nowrap;flex:0 0 auto;transition:.15s}
nav.nav a.on{background:var(--card2);color:var(--fg)}

/* 区块 */
section{margin:0 0 26px;scroll-margin-top:56px}
section>h2{font-size:14px;margin:0 0 11px;font-weight:640;color:var(--dim);
  display:flex;align-items:center;gap:8px;letter-spacing:.2px}
section>h2 .tag{font-size:10.5px;color:var(--dim2);font-weight:400;
  border:1px solid var(--line);padding:1px 8px;border-radius:20px;white-space:nowrap}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.grid2>*{min-width:0}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.grid3>*{min-width:0}
.card{background:var(--card);border-radius:var(--r);padding:16px 17px;min-width:0}
.card>h3{margin:0 0 8px;font-size:12px;color:var(--dim2);font-weight:600;
  display:flex;justify-content:space-between;gap:8px;letter-spacing:.3px}

/* 要闻 */
ol.top{list-style:none;counter-reset:n;margin:0;padding:0}
ol.top li{counter-increment:n;display:flex;gap:13px;padding:14px 0;
  border-bottom:1px solid var(--line)}
ol.top li:first-child{padding-top:2px}
ol.top li:last-child{border-bottom:0;padding-bottom:2px}
ol.top li>div{flex:1 1 auto;min-width:0}
ol.top li:before{content:counter(n);color:var(--dim2);font-size:11.5px;font-weight:700;
  min-width:14px;padding-top:4px;font-variant-numeric:tabular-nums;flex:0 0 auto}
.tt{font-size:16.5px;font-weight:600;line-height:1.45;overflow-wrap:anywhere;letter-spacing:-.1px}
.meta{margin-top:7px;font-size:11.5px;color:var(--dim2);display:flex;gap:6px;
  flex-wrap:wrap;align-items:center}
.orig{font-size:12px;color:var(--dim2);margin-top:4px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;max-width:100%;opacity:.75}
.badge{font-size:10.5px;padding:1.5px 7px;border-radius:7px;background:var(--card2);
  color:var(--dim);white-space:nowrap}
.badge.hot{background:rgba(255,107,138,.14);color:#ff9db0}
.badge.up{background:rgba(61,220,151,.12);color:#6ee7bf}
.badge.cn{background:rgba(109,143,255,.14);color:#a5b8ff}

/* 表格 → 移动端当列表 */
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--dim2);font-weight:500;font-size:10.5px;
  letter-spacing:.5px;padding:0 6px 8px;border-bottom:1px solid var(--line)}
td{padding:9px 6px;border-bottom:1px solid rgba(255,255,255,.045);vertical-align:middle}
tr:last-child td{border-bottom:0}
td.num{text-align:right;font-variant-numeric:tabular-nums;color:#c8d2e4;white-space:nowrap}
.rank{color:var(--acc);font-weight:700;font-variant-numeric:tabular-nums;width:20px}
.bar{height:3px;border-radius:3px;background:rgba(255,255,255,.07);margin-top:6px;overflow:hidden}
.bar i{display:block;height:100%;border-radius:3px;
  background:linear-gradient(90deg,#6d8fff,#3ddc97)}

/* 列表项 */
.item{padding:11px 0;border-bottom:1px solid var(--line);display:flex;
  justify-content:space-between;gap:12px;align-items:flex-start}
.item:first-child{padding-top:2px}
.item:last-child{border-bottom:0;padding-bottom:2px}
.item>div{flex:1 1 auto;min-width:0}
.item .n{font-size:14.5px;font-weight:550;line-height:1.45;overflow-wrap:anywhere}
.item .m{font-size:11.5px;color:var(--dim2);margin-top:4px}
.item .r{font-size:11px;white-space:nowrap;padding-top:2px;display:flex;gap:5px;
  flex:0 0 auto;font-variant-numeric:tabular-nums}
.desc{font-size:12.5px;color:var(--dim);margin-top:4px;line-height:1.5}
.subhead{margin:14px 0 2px;font-size:11px;color:var(--acc);font-weight:650;letter-spacing:.4px}
.subhead:first-child{margin-top:0}

/* 热榜 */
.hotrow{display:flex;gap:9px;align-items:baseline;padding:8px 0;
  border-bottom:1px solid rgba(255,255,255,.045);font-size:13.5px}
.hotrow:first-child{padding-top:2px}
.hotrow:last-child{border-bottom:0}
.hr{color:var(--dim2);font-weight:700;font-size:11px;min-width:13px;
  font-variant-numeric:tabular-nums;flex:0 0 auto}
.ht{flex:1;line-height:1.42;overflow-wrap:anywhere;min-width:0}
.hv{color:var(--dim2);font-size:10.5px;white-space:nowrap;flex:0 0 auto}

/* 榜单入口 */
.hub{display:flex;flex-wrap:wrap;gap:7px}
.hub a{font-size:12.5px;padding:6px 12px;border-radius:11px;background:var(--card2);
  color:#c0cbe0}

footer{border-top:1px solid var(--line);margin-top:26px;padding-top:16px;font-size:11px;
  color:var(--dim2);display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap}
.srcstat{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px;max-width:560px}
.srcstat span{font-size:10px;padding:2px 7px;border-radius:20px;background:var(--card2);
  color:var(--dim2)}
.srcstat span.bad{color:#ff9db0}
.srcstat span.ok{color:#8fd9c0}
code{background:var(--card2);padding:1px 6px;border-radius:5px;font-size:10.5px;color:#c0cbe0}

/* 底部 tab（App 感） */
.tabbar{position:fixed;left:0;right:0;bottom:0;z-index:30;display:none;
  background:rgba(10,11,14,.9);-webkit-backdrop-filter:blur(18px);backdrop-filter:blur(18px);
  border-top:1px solid var(--line);padding:7px 0 calc(7px + env(safe-area-inset-bottom))}
.tabbar a{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;
  font-size:10px;color:var(--dim2)}
.tabbar a .i{font-size:18px;line-height:1}
.tabbar a.on{color:var(--acc)}

@media(max-width:820px){
  .wrap{padding:0 15px}
  header{padding:20px 0 2px}
  h1{font-size:23px}
  .stamp{display:none}
  .stats{grid-template-columns:1fr 1fr;gap:9px;margin:14px 0}
  .stat{padding:12px 13px}
  .stat .v{font-size:21px}
  .grid2,.grid3{grid-template-columns:1fr}
  nav.nav{margin:0 -15px 16px;padding:8px 15px}
  body{padding-bottom:calc(84px + env(safe-area-inset-bottom))}
  .tabbar{display:flex}
  .card{padding:15px 14px;border-radius:16px}
  .tt{font-size:16px}
  /* 表格变列表：隐藏表头，每行一条 */
  .t-aa thead,.t-val thead{display:none}
  .t-aa tr,.t-val tr{display:flex;align-items:center;gap:9px;padding:11px 0;
    border-bottom:1px solid rgba(255,255,255,.05)}
  .t-aa tr:last-child,.t-val tr:last-child{border-bottom:0}
  .t-aa td,.t-val td{border:0;padding:0}
  .t-aa td:nth-child(2),.t-val td:nth-child(2){flex:1 1 auto;min-width:0;font-size:14px}
  .t-aa td:nth-child(4),.t-aa td:nth-child(6),
  .t-aa th:nth-child(4),.t-aa th:nth-child(6){display:none}
  .t-val td:nth-child(4){color:var(--dim2);font-size:12px}
  .t-val td:nth-child(5){color:var(--acc);font-size:12px;font-weight:600}
  table{min-width:0}
  .card{overflow:visible}
}
@media(max-width:420px){
  .stats{grid-template-columns:1fr 1fr}
}
"""



def card(title, body, tag=""):
    t = '<span class="tag">%s</span>' % E(tag) if tag else ""
    return '<div class="card"><h3><span>%s</span>%s</h3>%s</div>' % (E(title), t, body)


def render(headlines, aa, or_new, hn, reddit, gh, hf, ph, papers, zh, status,
           hot_cn=None, tr=None):
    tr = tr or {}
    hot_cn = hot_cn or []

    def bi(text, url):
        """外文标题：中文在上、原文小字在下；中文内容原样。"""
        if not text:
            return ""
        if not has_cjk(text):
            z = tr.get(text)
            if z and z.strip().lower() != text.strip().lower():
                return rel(url, z) + '<div class="orig">%s</div>' % E(text[:120])
        return rel(url, text)
    total = (len(headlines) + len(hn) + len(reddit) + len(gh) + len(hf)
             + len(ph) + len(papers) + len(zh))
    resonance = [h for h in headlines if len(h["sources"]) > 1]
    top_model = aa[0] if aa else None
    cutoff_date = (NOW - timedelta(days=SHEET["aa_new_days"])).strftime("%Y-%m-%d")
    new_models = [m for m in aa if m["release_date"] and m["release_date"] >= cutoff_date]
    ok_src = sum(1 for s in status.values() if not s.get("err"))

    stats_html = """
<div class="stats">
  <div class="stat"><div class="k">今日收录</div><div class="v">%d</div>
    <div class="d">条 · %d 个源正常</div></div>
  <div class="stat"><div class="k">跨源共振</div><div class="v">%d</div>
    <div class="d">多个社区同时推的同一件事</div></div>
  <div class="stat"><div class="k">当前最强</div><div class="v" style="font-size:18px;padding-top:6px">%s</div>
    <div class="d">智能指数 %s</div></div>
  <div class="stat"><div class="k">近 %d 天新模型</div><div class="v">%d</div>
    <div class="d">开权重 %d 个</div></div>
</div>""" % (total, ok_src, len(resonance),
             E(top_model["short"]) if top_model else "—",
             top_model["idx"] if top_model else "—",
             SHEET["aa_new_days"], len(new_models),
             sum(1 for m in new_models if m["open"]))

    head = ""
    for h in headlines:
        badges = []
        if len(h["sources"]) > 1:
            badges.append('<span class="badge hot">共振</span>')
        for s in list(h["sources"])[:3]:
            cls = "badge cn" if any("\u4e00" <= ch <= "\u9fff" for ch in s) else "badge"
            badges.append('<span class="%s">%s</span>' % (cls, E(s)))
        if h.get("points"):
            badges.append('<span class="badge up">▲ %d</span>' % h["points"])
        when = ago(h.get("published"))
        if when:
            exact = h["published"].strftime("%Y-%m-%d %H:%M") if h.get("published") else ""
            badges.append('<span class="badge" title="%s">%s</span>' % (E(exact), E(when)))
        head += ('<li><div><div class="tt">%s</div><div class="meta">%s</div></div></li>'
                 % (bi(h["title"], h["url"]), "".join(badges)))
    if not head:
        head = '<p style="color:#8b98b0">暂无要闻（可能所有源都超时，稍后再试）</p>'

    def aa_row(i, m):
        o = '<span class="badge">开源</span>' if m["open"] else ""
        est = ' <span class="badge">估</span>' if m["estimated"] else ""
        cost = "$%.2f" % m["cost"] if m["cost"] is not None else "—"
        spd = str(m["speed"]) if m["speed"] else "—"
        return ('<tr><td class="rank">%d</td>'
                '<td>%s %s%s<div class="bar"><i style="width:%d%%"></i></div></td>'
                '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
                '<td class="num" style="color:#8b98b0">%s</td></tr>'
                % (i, rel("https://artificialanalysis.ai/", m["short"]), o, est,
                   min(100, m["idx"]), m["idx"], spd, cost,
                   E(m["release_date"] or "")))

    aa_table = ('<table class="t-aa"><thead><tr><th>#</th><th>模型</th><th>智能</th>'
                '<th>tok/s</th><th>成本$</th><th>发布</th></tr></thead><tbody>')
    for i, m in enumerate(aa[: SHEET["aa_top"]], 1):
        aa_table += aa_row(i, m)
    aa_table += "</tbody></table>"

    new_sorted = sorted(new_models, key=lambda m: -m["idx"])[:10]
    new_table = ('<table class="t-aa"><thead><tr><th>#</th><th>新模型</th><th>智能</th>'
                 '<th>tok/s</th><th>成本$</th><th>发布</th></tr></thead><tbody>')
    if new_sorted:
        for i, m in enumerate(new_sorted, 1):
            new_table += aa_row(i, m)
    else:
        new_table += '<tr><td colspan="6" style="color:#8b98b0">暂无</td></tr>'
    new_table += "</tbody></table>"

    value = [m for m in aa if m["cost"] and m["cost"] > 0 and m["idx"] > 20]
    value.sort(key=lambda m: -(m["idx"] / m["cost"]))
    val_table = ('<table class="t-val"><thead><tr><th>#</th><th>模型</th><th>智能</th>'
                 '<th>成本$</th><th>性价比</th></tr></thead><tbody>')
    for i, m in enumerate(value[: SHEET["aa_value"]], 1):
        val_table += ('<tr><td class="rank">%d</td><td>%s</td><td class="num">%s</td>'
                      '<td class="num">$%.2f</td><td class="num">%.0f</td></tr>'
                      % (i, E(m["short"]), m["idx"], m["cost"], m["idx"] / m["cost"]))
    if not value:
        val_table += '<tr><td colspan="5" style="color:#8b98b0">暂无</td></tr>'
    val_table += "</tbody></table>"

    hn_html = ""
    for h in hn[: SHEET["hn_count"]]:
        hn_html += ('<div class="item"><div><div class="n">%s</div>'
                    '<div class="m">%s · %d 条讨论 · %s</div></div></div>'
                    % (bi(h["title"], h["url"]),
                       rel(h["hn_url"], "%d 分" % h["points"]),
                       h["comments"], E(ago(h["published"]))))
    if not hn_html:
        hn_html = '<p style="color:#8b98b0">暂无</p>'

    reddit_html = ""
    by_sub = {}
    for r in reddit:
        by_sub.setdefault(r["sub"], []).append(r)
    for cfg in CONF["reddit_subs"]:
        items = by_sub.get(cfg["label"], [])[: SHEET["reddit_per_sub"]]
        if not items:
            continue
        reddit_html += '<div class="subhead">%s</div>' % E(cfg["label"])
        for r in items:
            stale = ' <span class="badge">缓存</span>' if r.get("stale") else ""
            reddit_html += ('<div class="item"><div><div class="n" style="font-size:13.5px">%s%s</div>'
                            '<div class="m">%s</div></div></div>'
                            % (bi(r["title"], r["url"]), stale, E(ago(r["published"]))))
    if not reddit_html:
        reddit_html = ('<p style="color:#8b98b0">Reddit 暂时限流（它对高频访问很敏感）。'
                       '明天会自动重试，也可以稍后手动 <code>bash run-now.command</code>。</p>')

    gh_html = ""
    for r in gh[: SHEET["gh_count"]]:
        gh_html += ('<div class="item"><div><div class="n">%s</div>'
                    '<div class="desc">%s</div><div class="m">%s ★ %s</div></div>'
                    '<div class="r"><span class="badge up">+%d</span></div></div>'
                    % (rel(r["url"], r["repo"]), E(r["desc"][:150]), E(r["lang"]),
                       E(r["stars_total"]), r["stars_today"]))
    if not gh_html:
        gh_html = '<p style="color:#8b98b0">今日趋势榜里没有明显 AI 项目</p>'

    hf_html = ""
    for m in hf:
        hf_html += ('<div class="item"><div><div class="n">%s</div>'
                    '<div class="m">%s</div></div>'
                    '<div class="r"><span class="badge">♥ %d</span>'
                    '<span class="badge">↓ %s</span></div></div>'
                    % (rel(m["url"], m["id"]), E(m["task"] or m["lib"]),
                       m["likes"], "{:,}".format(m["downloads"])))
    if not hf_html:
        hf_html = '<p style="color:#8b98b0">暂无</p>'

    ph_html = ""
    for p in ph:
        flag = ' <span class="badge hot">AI</span>' if is_ai(p["title"] + " " + p["summary"]) else ""
        ph_html += ('<div class="item"><div><div class="n">%s%s</div>'
                    '<div class="desc">%s</div></div></div>'
                    % (bi(p["title"], p["url"]), flag, E(p["summary"][:110])))
    if not ph_html:
        ph_html = '<p style="color:#8b98b0">暂无</p>'

    or_html = ('<table><thead><tr><th>新上架模型</th><th>上下文</th>'
               '<th>输入$/M</th><th>输出$/M</th><th>上架</th></tr></thead><tbody>')
    for m in or_new:
        ctx = "%.0fK" % (m["ctx"] / 1000) if m["ctx"] else "—"
        or_html += ('<tr><td>%s</td><td class="num">%s</td><td class="num">%s</td>'
                    '<td class="num">%s</td><td class="num" style="color:#8b98b0">%s</td></tr>'
                    % (E(m["name"][:52]), ctx,
                       ("%.3f" % m["pin"]).rstrip("0").rstrip(".") if m["pin"] else "免费",
                       ("%.3f" % m["pout"]).rstrip("0").rstrip(".") if m["pout"] else "免费",
                       m["created"].strftime("%m-%d") if m["created"] else "—"))
    if not or_new:
        or_html += '<tr><td colspan="5" style="color:#8b98b0">暂无</td></tr>'
    or_html += "</tbody></table>"

    paper_html = ""
    for p in papers:
        paper_html += ('<div class="item"><div><div class="n" style="font-size:13.5px">%s</div>'
                       '<div class="desc">%s</div></div></div>'
                       % (bi(p["title"], p["url"]), E(p["summary"][:150])))
    if not paper_html:
        paper_html = '<p style="color:#8b98b0">暂无</p>'

    zh_html = ""
    for z in zh:
        zh_html += ('<div class="item"><div><div class="n" style="font-size:13.5px">%s</div>'
                    '<div class="m">%s · %s</div></div></div>'
                    % (rel(z["url"], z["title"]), E(z["source"]), E(ago(z["published"]))))
    if not zh_html:
        zh_html = '<p style="color:#8b98b0">暂无</p>'

    hot_html = ""
    if hot_cn:
        groups = {}
        for it in hot_cn:
            groups.setdefault(it["platform"], []).append(it)
        cells = ""
        for _, label in HOT_PLATFORMS:
            items = groups.get(label, [])
            if not items:
                continue
            rows = ""
            for it in items:
                flag = ' <span class="badge hot">AI</span>' if is_ai(it["title"]) else ""
                hv = '<span class="hv">%s</span>' % E(it["hot"]) if it.get("hot") else ""
                rows += ('<div class="hotrow"><span class="hr">%d</span>'
                         '<a class="ht" href="%s" target="_blank" rel="noopener">%s</a>%s%s</div>'
                         % (it["rank"], E(it["url"] or "#"), E(it["title"]), flag, hv))
            cells += card(label, rows, "Top %d" % len(items))
        hot_html = '<div class="grid3">%s</div>' % cells
    if not hot_html:
        hot_html = card("中文热榜", '<p style="color:#8b98b0">各平台接口暂时不可达，'
                                    '下一轮会自动重试。</p>')

    nav_items = [("s-news", "🔥 要闻"), ("s-model", "📊 模型"),
                 ("s-comm", "🗣️ 社区"), ("s-hotcn", "🇨🇳 热榜"),
                 ("s-open", "🚀 开源"), ("s-tool", "🧰 工具"),
                 ("s-paper", "📄 论文")]
    nav = '<nav class="nav">%s</nav>' % "".join(
        '<a href="#%s">%s</a>' % (i, E(t)) for i, t in nav_items)

    hub = "".join(rel(u, n) for n, u in HUB_LINKS)

    src = ""
    for k, v in status.items():
        cls = "bad" if v.get("err") else "ok"
        src += '<span class="%s" title="%s">%s %d</span>' % (
            cls, E(v.get("err") or "")[:120], E(v.get("name", k)), v.get("n", 0))

    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(title)s · %(today)s</title>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="AI 情报日报">
<meta name="theme-color" content="#0b0e14">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<style>%(css)s</style></head><body>
<div class="wrap">
<header>
  <div>
    <h1>AI <em>情报日报</em></h1>
    <div class="sub">%(subtitle)s · %(today)s</div>
  </div>
  <div class="stamp">生成于 <b>%(hhmm)s</b><br>社区榜单 / 官方发布 / 模型评测<br>数据每小时自动更新</div>
</header>
%(stats)s
<input id="q" placeholder="筛选：模型名 / 公司 / 关键词…">
%(nav)s

<section id="s-news"><h2>🔥 今日要闻 <span class="tag">跨源加权 · 近 %(hh)s 小时</span></h2>
<div class="card"><ol class="top">%(head)s</ol></div></section>

<section id="s-model"><h2>📊 模型战力榜 <span class="tag">Artificial Analysis 智能指数</span></h2>
<div class="grid2">%(c_aa)s%(c_val)s</div>
<div style="margin-top:14px">%(c_new)s</div>
</section>

<section id="s-comm"><h2>🗣️ 社区热榜 <span class="tag">Hacker News · Reddit</span></h2>
<div class="grid2">%(c_hn)s%(c_reddit)s</div></section>

<section id="s-hotcn"><h2>🇨🇳 全网热榜 <span class="tag">百度 · 头条 · B站 · V2EX · 掘金</span></h2>
%(c_hotcn)s</section>

<section id="s-open"><h2>🚀 开源与新品 <span class="tag">GitHub · Hugging Face · Product Hunt</span></h2>
<div class="grid2">%(c_gh)s%(c_hf)s</div>
<div style="margin-top:14px">%(c_ph)s</div></section>

<section id="s-tool"><h2>🧰 工具与方案 <span class="tag">新上架模型 · 价格 · 上下文</span></h2>
%(c_or)s
<div style="margin-top:14px">%(c_hub)s</div>
</section>

<section id="s-zh"><h2>🇨🇳 中文前沿 <span class="tag">量子位 · IT之家 · 中文快讯</span></h2>
%(c_zh)s</section>

<section id="s-paper"><h2>📄 论文速览 <span class="tag">arXiv cs.AI / cs.CL</span></h2>
%(c_paper)s</section>

<footer>
  <div>数据源健康度<div class="srcstat">%(src)s</div></div>
  <div style="text-align:right;line-height:1.9">
  常驻地址 <code>http://%(lan)s:8787</code> · <a href="/sources">数据源说明</a><br>
  手动刷新 <code>bash run-now.command</code><br>
  RSS <a href="/feed.xml">/feed.xml</a>（可丢进 Folo / FreshRSS）</div>
</footer>
</div>
<div class="tabbar">
  <a href="#s-news" data-t="s-news"><span class="i">🔥</span>要闻</a>
  <a href="#s-model" data-t="s-model"><span class="i">📊</span>模型</a>
  <a href="#s-hotcn" data-t="s-hotcn"><span class="i">🇨🇳</span>热榜</a>
  <a href="#s-tool" data-t="s-tool"><span class="i">🧰</span>工具</a>
</div>
<script>
var q=document.getElementById('q');
q.addEventListener('input',function(){
  var v=q.value.toLowerCase().trim();
  var els=document.querySelectorAll('.item, ol.top li, tbody tr');
  for(var i=0;i<els.length;i++){
    var el=els[i];
    el.style.display=(!v||el.innerText.toLowerCase().indexOf(v)>=0)?'':'none';
  }
});

/* 滚动时高亮当前板块（顶部导航 + 底部 tab） */
(function(){
  var secs=[].slice.call(document.querySelectorAll('section[id]'));
  var marks={};
  [].forEach.call(document.querySelectorAll('nav.nav a,.tabbar a'),function(a){
    marks[a.getAttribute('href').slice(1)]=a;
  });
  function sync(){
    var y=window.scrollY+120, cur=secs.length?secs[0].id:null;
    secs.forEach(function(s){ if(s.offsetTop<=y) cur=s.id; });
    for(var k in marks) marks[k].classList.toggle('on',k===cur);
  }
  window.addEventListener('scroll',sync,{passive:true});
  window.addEventListener('resize',sync);
  sync();
})();
</script>
</body></html>""" % {
        "title": E(CONF["title"]), "subtitle": E(CONF["subtitle"]), "today": TODAY,
        "css": CSS, "hhmm": NOW.strftime("%H:%M"), "stats": stats_html,
        "hh": CONF["headline_hours"], "head": head,
        "c_aa": card("总榜 Top %d" % SHEET["aa_top"], aa_table, "越高越强"),
        "c_val": card("性价比榜", val_table, "指数 ÷ 每任务成本"),
        "c_new": card("近 %d 天新模型" % SHEET["aa_new_days"], new_table, "按实力排序"),
        "c_hn": card("Hacker News 今日 AI 热帖", hn_html, "按分数"),
        "c_reddit": card("Reddit 各社区今日 Top", reddit_html, "按投票"),
        "c_gh": card("GitHub 今日趋势 · AI", gh_html, "按今日新增 star"),
        "c_hf": card("Hugging Face 趋势模型", hf_html, "trendingScore"),
        "c_ph": card("Product Hunt 新品", ph_html, "最新上架"),
        "c_or": card("近 %d 天新上架模型（OpenRouter 价目）" % SHEET["or_new_days"],
                     or_html, "选型参考"),
        "c_hub": card("常看榜单入口", '<div class="hub">%s</div>' % hub, "点开直达"),
        "c_zh": card("中文 AI 动态", zh_html),
        "c_paper": card("今日值得扫一眼的论文", paper_html),
        "src": src,
        "lan": LAN_IP,
        "nav": nav,
        "c_hotcn": hot_html,
    }


# ---------------------------------------------------------------------------
# 附属产物：RSS 与数据源说明页
# ---------------------------------------------------------------------------
def write_feed(headlines):
    def rfc822(dt):
        return dt.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    items = []
    for h in headlines:
        title = h["title"]
        src = "/".join(sorted(h.get("sources") or []))
        if src:
            title = "%s（%s）" % (title, src)
        pub = h.get("published") or NOW
        items.append(
            "<item><title>%s</title><link>%s</link>"
            "<guid isPermaLink=\"false\">%s</guid><pubDate>%s</pubDate>"
            "<description>%s</description></item>"
            % (E(title), E(h["url"] or ""), E("%s-%s" % (TODAY, abs(hash(title)) % 10 ** 8)),
               rfc822(pub), E(h.get("summary") or "")))
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<rss version="2.0"><channel><title>AI 情报日报 · %s</title>'
           '<link>http://%s:8787/</link><description>全网 AI 社区榜单与最新消息</description>'
           '<language>zh-CN</language><lastBuildDate>%s</lastBuildDate>%s</channel></rss>'
           % (TODAY, LAN_IP, rfc822(NOW), "".join(items)))
    (OUT / "feed.xml").write_text(xml, encoding="utf-8")


def write_sources(status):
    rows = []
    for cfg in CONF["feeds"]:
        st = status.get(cfg["id"], {})
        rows.append((cfg["name"], "RSS/Atom", str(cfg["weight"]),
                     st.get("n", 0), st.get("err")))
    for cfg in CONF["reddit_subs"]:
        st = status.get("reddit_" + cfg["sub"], {})
        rows.append((cfg["label"], "Reddit 日榜", str(cfg["weight"]),
                     st.get("n", 0), st.get("err")))
    extra = [("Hacker News", "HN Algolia API", "32", status.get("hn", {}).get("n", 0), None),
             ("GitHub Trending", "页面解析", "—", status.get("github", {}).get("n", 0), None),
             ("Hugging Face", "HF API trendingScore", "—",
              status.get("hf", {}).get("n", 0), None),
             ("OpenRouter", "模型目录与价格 API", "—",
              status.get("openrouter", {}).get("n", 0), None),
             ("Artificial Analysis", "官网内嵌评测数据", "—",
              status.get("aa", {}).get("n", 0), None)]
    rows.extend(extra)

    tr = ""
    for name, kind, w, n, err in rows:
        badge = ('<span style="color:#ff9db0">失败</span>' if err
                 else '<span style="color:#6ee7bf">正常</span>')
        tr += ("<tr><td>%s</td><td>%s</td><td class='num'>%s</td>"
               "<td class='num'>%s</td><td>%s</td></tr>"
               % (E(name), E(kind), w, n, badge))

    page = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>数据源说明 · AI 情报日报</title><style>
body{background:#0b0e14;color:#e6ebf5;font:15px/1.7 -apple-system,"PingFang SC",sans-serif;
margin:0;padding:34px 20px}
.wrap{max-width:820px;margin:0 auto}
h1{font-size:22px;margin:0 0 6px}
p{color:#8b98b0;font-size:13px}
table{width:100%;border-collapse:collapse;margin-top:18px;font-size:13px}
th{text-align:left;color:#8b98b0;font-weight:500;font-size:11px;padding:0 8px 8px;
border-bottom:1px solid #1f2735;text-transform:uppercase;letter-spacing:.5px}
td{padding:8px;border-bottom:1px solid #171e2b}
td.num{text-align:right;font-variant-numeric:tabular-nums}
a{color:#5b8cff;text-decoration:none}
</style></head><body><div class="wrap">
<h1>数据源与可信度</h1>
<p>本页说明每一类信息来自哪里、权重多少（数字越大越容易上要闻）、今天抓到了多少条。
标题一律直链原文，不转述、不加工，点进去就是一手页面。</p>
<table><thead><tr><th>来源</th><th>类型</th><th>权重</th><th>今日条数</th><th>状态</th></tr></thead>
<tbody>__ROWS__</tbody></table>
<p style="margin-top:22px">排序口径：来源权重 × 时近衰减（14 小时半衰）× 互动加成 × 跨源共振系数。
同一事件被多个源同时报道时加权上浮，并在面板上标「共振」。</p>
<p><a href="/">← 回到今天的日报</a></p>
</div></body></html>""".replace("__ROWS__", tr)
    (OUT / "sources.html").write_text(page, encoding="utf-8")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    open_browser = "--open" in sys.argv
    quiet = "--quiet" in sys.argv
    t0 = time.time()

    collectors = [("feeds", collect_feeds), ("reddit", collect_reddit),
                  ("hn", collect_hn), ("github", collect_github),
                  ("hf", collect_hf), ("ph", collect_producthunt),
                  ("openrouter", collect_openrouter), ("aa", collect_aa),
                  ("arxiv", collect_arxiv), ("hotcn", collect_hot_cn)]

    results, status = {}, {}
    with ThreadPoolExecutor(max_workers=9) as ex:
        futs = {ex.submit(fn): name for name, fn in collectors}
        for f in as_completed(futs):
            name = futs[f]
            try:
                items, st = f.result()
            except Exception as e:  # noqa: BLE001
                items, st = [], {name: {"name": name, "n": 0, "err": str(e)}}
                log("%s 崩溃: %s" % (name, traceback.format_exc(limit=3)))
            results[name] = items
            for k, v in st.items():
                status[k] = v

    feeds = results.get("feeds", [])
    headlines = build_headlines(feeds, results.get("hn", []), results.get("reddit", []))
    zh = build_zh(feeds)
    papers = build_papers(results.get("arxiv", []))
    aa = results.get("aa", [])
    or_new = results.get("openrouter", [])
    hn = results.get("hn", [])
    reddit = results.get("reddit", [])
    gh = sorted(results.get("github", []), key=lambda r: -r["stars_today"])
    hf = results.get("hf", [])
    ph = results.get("ph", [])

    hot_cn = results.get("hotcn", [])

    # 只翻译外文标题，已翻过的走本地缓存
    want_titles = ([h["title"] for h in headlines] + [h["title"] for h in hn]
                   + [r["title"] for r in reddit] + [p["title"] for p in ph]
                   + [p["title"] for p in papers])
    try:
        tr_map = translate_titles(want_titles)
    except Exception as e:  # noqa: BLE001
        log("翻译失败，保留原文：%s" % e)
        tr_map = {}

    page = render(headlines, aa, or_new, hn, reddit, gh, hf, ph, papers, zh, status,
                  hot_cn=hot_cn, tr=tr_map)
    for name in ("index.html", TODAY + ".html"):
        (OUT / name).write_text(page, encoding="utf-8")

    snapshot = {
        "generated_at": NOW.isoformat(),
        "headlines": [_plain(h) for h in headlines],
        "models": aa,
        "openrouter_new": [_plain(m) for m in or_new],
        "hn": [_plain(h) for h in hn],
        "reddit": [_plain(r) for r in reddit],
        "github": gh,
        "hf": [_plain(m) for m in hf],
        "producthunt": [_plain(p) for p in ph],
        "papers": [_plain(p) for p in papers],
        "zh": [_plain(z) for z in zh],
        "hot_cn": hot_cn,
        "translated": len(tr_map),
        "status": status,
        "elapsed": round(time.time() - t0, 1),
    }
    (DATA / (TODAY + ".json")).write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")

    write_feed(headlines)
    write_sources(status)

    ok = sum(1 for s in status.values() if not s.get("err"))
    bad = ["%s(%s)" % (v.get("name", k), v.get("err")) for k, v in status.items() if v.get("err")]
    log("完成：%d/%d 源成功，要闻 %d 条，耗时 %.1fs"
        % (ok, len(status), len(headlines), time.time() - t0))
    if bad:
        log("失败源：" + "; ".join(bad))
    (LOGS / "run.log").write_text("\n".join(log_lines[-400:]) + "\n", encoding="utf-8")

    if open_browser:
        marker = STATE / "last_open.txt"
        if not marker.exists() or marker.read_text().strip() != TODAY:
            subprocess.run(["open", str(OUT / "index.html")], check=False)
            marker.write_text(TODAY, encoding="utf-8")
            log("已自动打开浏览器")

    if not quiet:
        print("✅ %s 日报已生成 -> %s" % (TODAY, OUT / "index.html"))
        print("   要闻 %d | 模型 %d | 社区 %d | 中文热榜 %d | 仓库 %d | HF %d | 新品 %d | 论文 %d"
              % (len(headlines), len(aa), len(hn) + len(reddit), len(hot_cn), len(gh),
                 len(hf), len(ph), len(papers)))
        print("   已翻译 %d 条外文标题" % len(tr_map))
        print("   源 %d/%d 成功，耗时 %.1fs" % (ok, len(status), time.time() - t0))
        for b in bad:
            print("   ⚠️  " + b)


if __name__ == "__main__":
    main()
