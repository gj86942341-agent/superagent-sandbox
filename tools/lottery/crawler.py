#!/usr/bin/env python3
"""爬官方规则与"选号方法"页面，把内容结构化，并和系统内置的规则做一致性比对。

为什么值得爬
------------
系统里对奖用的 7 奖级、开奖时间、价格是**硬编码**的。官方一旦改规则（历史上改过两次：
2007 六奖级 → 九奖级 → 2026-02 七奖级），硬编码就会静默错下去。这个爬虫的作用是
**规则漂移检测**：定期抓官方页面，抽取关键字段，和内置值比对，不一致就报警。

同时提供 `methods` 模式：抓"冷热/遗漏/和值"这类选号方法的公开说明，整理成知识库，
每条都标注 `predictive: false`（数学上无效），避免把"方法"误当成"能力"。

用法
----
    python crawler.py rules            抓官方规则并比对
    python crawler.py methods          抓选号方法说明
    python crawler.py all
    python crawler.py rules --offline  只用上次抓的缓存做解析/比对

抓到的原始 HTML 存在 raw/ 下，便于复核；请求间隔 1.5 秒，只抓一两个页面，不批量扫站。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import lottery as lt  # noqa: E402  - 规则常量的唯一来源，避免爬虫和系统各写一份

RAW = os.path.join(BASE, "raw")
RULES_JSON = os.path.join(BASE, "rules.json")
METHODS_JSON = os.path.join(BASE, "methods.json")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9",
}
DELAY = 1.5

# 系统当前内置的规则：**直接从 lottery.py 读常量**，所以这里不是第二份真相。
# 官方页面一旦和它不一致，说明规则变了（历史上改过两次：2007 六奖级 → 九奖级 → 2026-02 七奖级）。
WEEKDAY_NAMES = "一二三四五六日"
EXPECTED = {
    "front": {"count": lt.FRONT_COUNT, "min": lt.FRONT_MIN, "max": lt.FRONT_MAX},
    "back": {"count": lt.BACK_COUNT, "min": lt.BACK_MIN, "max": lt.BACK_MAX},
    "draw_days": [WEEKDAY_NAMES[day] for day in sorted(lt.DRAW_WEEKDAYS)],
    "sale_close": f"{lt.SALE_CLOSE[0]:02d}:{lt.SALE_CLOSE[1]:02d}",
    "draw_time": f"{lt.DRAW_TIME[0]:02d}:{lt.DRAW_TIME[1]:02d}",
    "price_basic": lt.PRICE_BASIC,        # 每注基本投注
    "price_addon": lt.ADDON_PRICE,        # 追加投注追加的金额
    "price_total_add": lt.PRICE_ADD,      # 基本 + 追加
    "prize_levels": ["一等奖", "二等奖", "三等奖", "四等奖", "五等奖", "六等奖", "七等奖"],
}

RULES_SOURCES = [
    {"slug": "zjtycp-guize", "name": "浙江体彩·大乐透规则全文",
     "url": "https://www.zjtycp.cn/guize_cjdlt/", "charset": "utf-8",
     # 规则正文是这一页负责的字段
     "fields": ["front_min", "front_max", "front_count", "back_min", "back_max", "back_count",
                "draw_days_text", "draw_days", "price_basic", "price_addon", "price_total_add",
                "prize_levels_found", "conditions_found"]},
    {"slug": "hnlottery-time", "name": "河南体彩·开奖与停售时间调整公告",
     "url": "https://hnlottery.com.cn/2023/02-22/13561.html", "charset": "utf-8",
     # 时间只认这份公告：规则页里出现的其他时间（直播/兑奖）会干扰
     "fields": ["sale_close", "draw_time"]},
    {"slug": "sporttery-help-2858", "name": "中国体彩网·旧版规则（历史参考）",
     "url": "https://www.sporttery.cn/help/2858.html?gid=1", "charset": "gb18030",
     # 旧版是九奖级，只作历史留档，不参与一致性比对
     "fields": []},
]
METHOD_SOURCES = [
    {"slug": "lottery-terms", "name": "彩票术语与常见指标说明",
     "url": "https://blog.dktoolset.com/posts/lottery/lottery-terms-explained.html", "charset": "utf-8"},
]


# ----------------------------------------------------------------------------- 抓取
def fetch(url: str, charset: str, timeout: float = 20.0) -> tuple[str, dict]:
    request = urllib.request.Request(url, headers={**HEADERS, "Referer": urllib.parse.urlsplit(url).scheme
                                                   + "://" + urllib.parse.urlsplit(url).netloc + "/"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        status = response.status
        info = {"http": status, "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest()[:16],
                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    for encoding in (charset, "utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
            if "<" in text or len(text) > 50:
                info["decoded_as"] = encoding
                return text, info
        except UnicodeDecodeError:
            continue
    info["decoded_as"] = "replace"
    return raw.decode("utf-8", "replace"), info


def save_raw(slug: str, html: str) -> str:
    os.makedirs(RAW, exist_ok=True)
    path = os.path.join(RAW, f"{slug}-{datetime.now():%Y%m%d}.html")
    with open(path, "w", encoding="utf-8", errors="replace") as handle:
        handle.write(html)
    return os.path.relpath(path, BASE)


def cached_raw(slug: str) -> str | None:
    if not os.path.isdir(RAW):
        return None
    files = sorted((f for f in os.listdir(RAW) if f.startswith(slug + "-")), reverse=True)
    if not files:
        return None
    with open(os.path.join(RAW, files[0]), "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


# ----------------------------------------------------------------------------- HTML -> 文本
class TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg"}
    BLOCK = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "table", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


def to_text(html: str) -> str:
    parser = TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - malformed pages are common
        pass
    text = "".join(parser.parts)
    # 部分站点在标题里插零宽空格（\u200b），会让"标题行"匹配失败并串到下一段
    text = text.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


# ----------------------------------------------------------------------------- 字段抽取
def first(patterns: list[str], text: str, flags: int = 0) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return match.group(1).strip()
    return None


def extract_rules(text: str) -> dict:
    """从规则正文里抽字段。抽不到就给 None，交给比对环节标出'未能验证'。"""
    compact = re.sub(r"\s+", "", text)
    found: dict = {}

    front = re.search(r"前区[^。；]{0,30}?第?五个?(?:号码|号)?[^。；]{0,20}?从?(\d{1,2})[至到\-~—](\d{1,2})", compact) \
        or re.search(r"前区[^。；]{0,20}?(\d{1,2})[至到\-~—](\d{1,2})", compact)
    if front:
        found["front_min"] = int(front.group(1))
        found["front_max"] = int(front.group(2))
    if re.search(r"前区[^。；]{0,10}?选5|前区.{0,4}5个", compact):
        found["front_count"] = 5

    back = re.search(r"后区[^。；]{0,20}?(\d{1,2})[至到\-~—](\d{1,2})", compact)
    if back:
        found["back_min"] = int(back.group(1))
        found["back_max"] = int(back.group(2))
    if re.search(r"后区[^。；]{0,10}?选2|后区.{0,4}2个", compact):
        found["back_count"] = 2

    days = re.search(r"每周([一二三四五六日天、和及]{2,12})开奖", compact) \
        or re.search(r"每周([一二三四五六日天、和及]{2,12})", compact)
    if days:
        found["draw_days_text"] = days.group(1)
        found["draw_days"] = [ch for ch in found["draw_days_text"] if ch in "一二三四五六日"]

    def latest_time(*labels: str) -> str | None:
        """取"停售/开奖"子句里最新的时间。

        公告常写成"销售截止时间由 20:00 调整为 21:00，开奖时间由 20:25 调整为 21:25"：
        两个时间在同一句里，所以先按逗号切成子句，再看子句里最后一个时间。
        只接受傍晚 19:00–23:59 的时间，避开页面上的其他时间（直播、兑奖、别的彩种）。
        """
        candidates: list[str] = []
        for clause in re.split(r"[。；\n，,]", compact):
            if not any(label in clause for label in labels):
                continue
            for match in re.finditer(r"(\d{1,2})[:：](\d{2})", clause):
                hour, minute = int(match.group(1)), int(match.group(2))
                if 19 <= hour <= 23:
                    candidates.append(f"{hour:02d}:{minute:02d}")
            for match in re.finditer(r"(\d{1,2})时(\d{1,2})分", clause):
                hour, minute = int(match.group(1)), int(match.group(2))
                if 19 <= hour <= 23:
                    candidates.append(f"{hour:02d}:{minute:02d}")
        return candidates[-1] if candidates else None

    found["sale_close"] = latest_time("停售", "销售截止", "停止销售")
    found["draw_time"] = latest_time("开奖时间", "摇奖", "开奖")

    basic = re.search(r"每注基本投注[^。；]{0,12}?(\d)元", compact) \
        or re.search(r"基本投注[^。；]{0,12}?人民币(\d)元", compact)
    if basic:
        found["price_basic"] = int(basic.group(1))
    addon = re.search(r"追加投注[^。；]{0,12}?(?:人民币)?(\d)元", compact)
    if addon:
        found["price_addon"] = int(addon.group(1))
    if found.get("price_basic") and found.get("price_addon"):
        found["price_total_add"] = found["price_basic"] + found["price_addon"]

    levels = [name for name in EXPECTED["prize_levels"] if name in text]
    found["prize_levels_found"] = levels

    combos = sorted(set(re.findall(r"(\d)\+(\d)", compact)))
    if combos:
        found["conditions_found"] = [f"{a}+{b}" for a, b in combos]
    return found


def extract_methods(text: str) -> list[dict]:
    """把方法页抽成 {name, definition}。

    页面结构是"标题行（方法名，可能带英文括注）"+ 若干"是什么：/怎么算：/怎么用："行，
    以及开头一段"方法名 / 一行定义"的速览。只认标题行，避免把正文例句当成方法。
    """
    keywords = {"冷热号": "冷热号", "冷热温号": "冷热号", "遗漏值": "遗漏", "和值": "和值",
                "奇偶比": "奇偶比", "大小比": "大小比", "区间分布": "区间", "连号": "连号",
                "重号": "重号", "跨度": "跨度", "012路": "012路", "AC值": "AC值",
                "质合比": "质合比", "尾数": "尾数", "龙头凤尾": "龙头凤尾"}
    headings = {name: re.compile(r"^" + re.escape(name) + r"(?:（[^）]*）)?\s*$") for name in keywords}
    collected: dict[str, list[str]] = {}
    current: str | None = None
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    for line in lines:
        heading = None
        for name, pattern in headings.items():
            if len(line) <= 40 and pattern.match(line):
                heading = keywords[name]
                break
        if heading:
            current = heading
            collected.setdefault(current, [])
            continue
        if current is None:
            continue
        for prefix in ("是什么：", "怎么算：", "怎么用：", "是什么:", "怎么算:", "怎么用:"):
            if line.startswith(prefix):
                body = line[len(prefix):].strip()
                if body and body not in collected[current]:
                    collected[current].append(body)
                break
        else:
            # 速览段：标题下一行就是一行定义
            if not collected[current] and 4 <= len(line) <= 60:
                collected[current].append(line)
    return [{"name": name, "definition": "；".join(parts)[:400],
             "predictive": False,
             "note": "历史统计指标。大乐透每期独立等概率，对下期没有预测力；"
                     "用它选号只改变号码形状，不改变中奖概率。"}
            for name, parts in collected.items() if parts]


# ----------------------------------------------------------------------------- 比对
def compare(parsed: dict) -> list[dict]:
    checks: list[dict] = []

    def check(name: str, expected, got, ok: bool | None):
        checks.append({"field": name, "expected": expected, "crawled": got,
                       "status": "ok" if ok else ("unknown" if got is None else "MISMATCH")})

    front = parsed.get("front_min"), parsed.get("front_max")
    check("前区范围", f"{EXPECTED['front']['min']}-{EXPECTED['front']['max']}",
          f"{front[0]}-{front[1]}" if front[0] else None,
          front == (EXPECTED["front"]["min"], EXPECTED["front"]["max"]) if front[0] else None)
    back = parsed.get("back_min"), parsed.get("back_max")
    check("后区范围", f"{EXPECTED['back']['min']}-{EXPECTED['back']['max']}",
          f"{back[0]}-{back[1]}" if back[0] else None,
          back == (EXPECTED["back"]["min"], EXPECTED["back"]["max"]) if back[0] else None)
    check("前区个数", EXPECTED["front"]["count"], parsed.get("front_count"),
          parsed.get("front_count") == EXPECTED["front"]["count"] if parsed.get("front_count") else None)
    check("后区个数", EXPECTED["back"]["count"], parsed.get("back_count"),
          parsed.get("back_count") == EXPECTED["back"]["count"] if parsed.get("back_count") else None)
    days = parsed.get("draw_days")
    check("开奖日", "周" + "/".join(EXPECTED["draw_days"]), days,
          sorted(days) == sorted(EXPECTED["draw_days"]) if days else None)
    check("停售时间", EXPECTED["sale_close"], parsed.get("sale_close"),
          parsed.get("sale_close") == EXPECTED["sale_close"] if parsed.get("sale_close") else None)
    check("开奖时间", EXPECTED["draw_time"], parsed.get("draw_time"),
          parsed.get("draw_time") == EXPECTED["draw_time"] if parsed.get("draw_time") else None)
    check("基本投注价", EXPECTED["price_basic"], parsed.get("price_basic"),
          parsed.get("price_basic") == EXPECTED["price_basic"] if parsed.get("price_basic") else None)
    check("追加加价", EXPECTED["price_addon"], parsed.get("price_addon"),
          parsed.get("price_addon") == EXPECTED["price_addon"] if parsed.get("price_addon") else None)
    check("追加总价", EXPECTED["price_total_add"], parsed.get("price_total_add"),
          parsed.get("price_total_add") == EXPECTED["price_total_add"]
          if parsed.get("price_total_add") else None)
    levels = parsed.get("prize_levels_found")
    check("奖级", EXPECTED["prize_levels"], levels,
          sorted(levels) == sorted(EXPECTED["prize_levels"]) if levels else None)
    return checks


def crawl_rules(offline: bool = False) -> dict:
    out: dict = {"fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "sources": [], "parsed": {},
                 "checks": [], "verdict": "unknown"}
    merged: dict = {}
    for source in RULES_SOURCES:
        html = None
        if offline:
            html = cached_raw(source["slug"])
            info = {"http": "cached", "bytes": len(html) if html else 0}
        else:
            try:
                html, info = fetch(source["url"], source["charset"])
                info["saved"] = save_raw(source["slug"], html)
                time.sleep(DELAY)
            except Exception as exc:  # noqa: BLE001
                info = {"error": f"{type(exc).__name__}: {exc}"}
        entry = {"name": source["name"], "url": source["url"], **info}
        if html:
            text = to_text(html)
            entry["text_chars"] = len(text)
            parsed = extract_rules(text)
            entry["parsed"] = parsed
            allowed = source.get("fields")
            for key, value in parsed.items():
                if allowed is not None and key not in allowed:
                    continue
                if key not in merged or merged[key] in (None, [], ""):
                    merged[key] = value
        out["sources"].append(entry)
    out["parsed"] = merged
    out["checks"] = compare(merged)
    bad = [c for c in out["checks"] if c["status"] == "MISMATCH"]
    known = [c for c in out["checks"] if c["status"] != "unknown"]
    if bad:
        out["verdict"] = "drift"
    elif known and len(known) == len(out["checks"]):
        out["verdict"] = "consistent"
    elif known:
        out["verdict"] = "partial"
    with open(RULES_JSON, "w", encoding="utf-8") as handle:
        json.dump(out, handle, ensure_ascii=False, indent=2)
    return out


def crawl_methods(offline: bool = False) -> dict:
    out: dict = {"fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "sources": [], "methods": []}
    for source in METHOD_SOURCES:
        html = None
        if offline:
            html = cached_raw(source["slug"])
        else:
            try:
                html, info = fetch(source["url"], source["charset"])
                info["saved"] = save_raw(source["slug"], html)
                time.sleep(DELAY)
            except Exception as exc:  # noqa: BLE001
                out["sources"].append({"name": source["name"], "url": source["url"],
                                       "error": f"{type(exc).__name__}: {exc}"})
                continue
        if not html:
            out["sources"].append({"name": source["name"], "url": source["url"], "error": "no cached copy"})
            continue
        text = to_text(html)
        methods = extract_methods(text)
        for method in methods:
            method["source"] = source["url"]
        out["sources"].append({"name": source["name"], "url": source["url"], "text_chars": len(text),
                              "methods_found": len(methods)})
        out["methods"].extend(methods)
    with open(METHODS_JSON, "w", encoding="utf-8") as handle:
        json.dump(out, handle, ensure_ascii=False, indent=2)
    return out


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "rules"
    offline = "--offline" in argv
    if command in {"rules", "all"}:
        result = crawl_rules(offline)
        print("== 规则抓取 ==")
        for source in result["sources"]:
            print(f"  {source['name']}: http={source.get('http')} bytes={source.get('bytes')} "
                  f"text={source.get('text_chars')} err={source.get('error')}")
        print(f"  解析结果: {json.dumps(result['parsed'], ensure_ascii=False)}")
        print(f"  {'字段':<10}{'系统内置':<16}{'抓取结果':<22}状态")
        for check in result["checks"]:
            print(f"  {check['field']:<10}{str(check['expected']):<16}{str(check['crawled']):<22}{check['status']}")
        print(f"  结论: {result['verdict']}")
    if command in {"methods", "all"}:
        result = crawl_methods(offline)
        print("\n== 方法抓取 ==")
        for source in result["sources"]:
            print(f"  {source.get('name')}: text={source.get('text_chars')} found={source.get('methods_found')} "
                  f"err={source.get('error')}")
        for method in result["methods"]:
            print(f"  - {method['name']}: {method['definition'][:80]}")
        print(f"  共 {len(result['methods'])} 条（全部标注 predictive=false）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
