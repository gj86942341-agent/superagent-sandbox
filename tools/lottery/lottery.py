#!/usr/bin/env python3
"""大乐透研究与记录系统（本地、只读、不代购）。

做什么
------
1. 从中国体彩网官方接口增量抓取开奖数据（号码 + 各奖级实发奖金）。
2. 开奖日按"研究偏好"生成 3 注号码并落库，作为当期预测记录。
3. 开奖后自动对奖：算命中个数、判奖级、取官方实发奖金、累计 ROI。
4. 输出"策略表现 vs 随机期望"的诚实对比。

重要的诚实声明（代码里每一处都遵守）
--------------------------------------
前区 35 选 5、后区 12 选 2，每期独立且等概率。**任何生成策略都不改变中奖概率**：
任意一注中一等奖 1/21,425,712，中任意奖约 6.67%。本系统是"研究与记录"工具，
不是预测工具；策略只决定"买哪一注"，不决定"开出什么"。

命令
----
    python lottery.py fetch        增量抓取开奖数据
    python lottery.py predict      为下一期生成 3 注并落库
    python lottery.py check        对尚未对奖的投注对奖
    python lottery.py report       累计投入 / 回收 / ROI / 策略对比
    python lottery.py status       机器可读状态（看板用）
    python lottery.py selftest     用 11 个官方用例自测对奖逻辑
    python lottery.py daily        fetch → check → (开奖日) predict，供计划任务调用
"""
from __future__ import annotations

import itertools
import json
import math
import os
import random
import sqlite3
import sys
import urllib.request
from datetime import date, datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
# The database may live outside the checkout (a scheduled task keeps the history in one
# place while the code comes from the repository). LOTTERY_DB overrides the default.
DB_PATH = os.environ.get("LOTTERY_DB") or os.path.join(BASE, "lottery.sqlite")
API = ("https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry"
       "?gameNo=85&provinceId=0&pageSize=100&isVerify=1&pageNo={page}")
# 官方接口反爬：必须同时带 Referer 与浏览器型 User-Agent，缺一返回 HTTP 567。
HEADERS = {
    "Referer": "https://www.sporttery.cn/",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
}
DRAW_WEEKDAYS = {0, 2, 5}          # 周一、周三、周六
SALE_CLOSE = (21, 0)               # 开奖日 21:00 停售
DRAW_TIME = (21, 25)               # 21:25 开奖
# 号码范围与注数：crawler.py 用这些常量去和官方页面比对，改动这里会立刻反映到比对结果里。
FRONT_MIN, FRONT_MAX, FRONT_COUNT = 1, 35, 5
BACK_MIN, BACK_MAX, BACK_COUNT = 1, 12, 2
PRICE_BASIC = 2                    # 每注基本投注
ADDON_PRICE = 1                    # 追加投注"追加"的金额（官方原文：每注追加投注金额人民币 1 元）
PRICE_ADD = PRICE_BASIC + ADDON_PRICE   # 追加票每注总价 3 元
FLOATING = ("一等奖", "二等奖")
# 冷热偏好回看的期数。30 期是彩民圈的常见口径，**没有证据表明 30 一定优于 50 或 100**；
# 把它写进策略名（hot-cold(30)）是为了将来能按参数比较表现，而不是因为 30 更"准"。
HOT_WINDOW = 30
# 每期的固定投注方案：一张 6+2 复式（6 注），带追加。
# 6 注 × (基本 2 元 + 追加 1 元) = 18 元；不追加则为 12 元。
TICKET_FRONT_N = 6
TICKET_BACK_N = 2
TICKET_ADD = True
# 单注一等奖的全部可能组合数：C(35,5) × C(12,2) = 21,425,712。
JACKPOT_COMBINATIONS = math.comb(FRONT_MAX, FRONT_COUNT) * math.comb(BACK_MAX, BACK_COUNT)
PRIZE_RANK = {"一等奖": 1, "二等奖": 2, "三等奖": 3, "四等奖": 4, "五等奖": 5, "六等奖": 6, "七等奖": 7}

SCHEMA = """
CREATE TABLE IF NOT EXISTS draws (
  draw_no    TEXT PRIMARY KEY,
  draw_date  TEXT NOT NULL,
  front      TEXT NOT NULL,
  back       TEXT NOT NULL,
  pool       REAL,
  sales      REAL,
  raw_time   TEXT
);
CREATE TABLE IF NOT EXISTS prizes (
  draw_no TEXT NOT NULL,
  level   TEXT NOT NULL,
  count   TEXT,
  amount  REAL,
  PRIMARY KEY (draw_no, level)
);
CREATE TABLE IF NOT EXISTS tickets (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  draw_no    TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  front      TEXT NOT NULL,
  back       TEXT NOT NULL,
  strategy   TEXT NOT NULL,
  add_flag   INTEGER NOT NULL DEFAULT 0,
  cost       INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
  ticket_id  INTEGER PRIMARY KEY,
  draw_no    TEXT NOT NULL,
  front_hit  INTEGER NOT NULL,
  back_hit   INTEGER NOT NULL,
  level      TEXT,
  amount     REAL NOT NULL DEFAULT 0,
  checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


# ----------------------------------------------------------------------------- 通用
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """就地补列，旧库不用重建。"""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
    if "kind" not in columns:
        conn.execute("ALTER TABLE tickets ADD COLUMN kind TEXT NOT NULL DEFAULT 'single'")
    if "bets" not in columns:
        conn.execute("ALTER TABLE tickets ADD COLUMN bets INTEGER NOT NULL DEFAULT 1")
    result_columns = {row["name"] for row in conn.execute("PRAGMA table_info(results)")}
    if "detail" not in result_columns:
        conn.execute("ALTER TABLE results ADD COLUMN detail TEXT")
    conn.commit()


# ----------------------------------------------------------------------------- 复式
def compound_plan(front_n: int, back_n: int, add: bool = False) -> dict:
    """复式方案的注数、成本与覆盖。

    注数 = C(前区个数,5) × C(后区个数,2)。复式**线性放大成本与覆盖，不改变每注的中奖概率**：
    每注仍是 1/21,425,712，期望返奖率仍是约 51%。

    追加只对一、二等奖加钱（按基本奖 80% 追加），所以追加后的成本是「注数 × (2+1) 元」。
    """
    if not (FRONT_COUNT <= front_n <= 18 and BACK_COUNT <= back_n <= 12):
        raise ValueError("复式范围：前区 5-18 个、后区 2-12 个")
    if front_n == FRONT_COUNT and back_n == BACK_COUNT:
        raise ValueError("5+2 是单式，不是复式")
    bets = math.comb(front_n, FRONT_COUNT) * math.comb(back_n, BACK_COUNT)
    unit = PRICE_BASIC + (ADDON_PRICE if add else 0)
    return {"label": f"{front_n}+{back_n}" + ("追加" if add else ""), "front": front_n, "back": back_n,
            "bets": bets, "unit": unit, "cost": bets * unit, "add": add,
            "jackpot_odds": bets / JACKPOT_COMBINATIONS,
            "cost_per_year": bets * unit * 156}


def plans_table() -> list[dict]:
    """常见复式方案的成本/覆盖表，供挑选。"""
    return [compound_plan(front, back) for front, back in
            ((6, 2), (6, 3), (7, 2), (7, 3), (8, 2), (8, 3))]


def now() -> datetime:
    return datetime.now()


def parse_numbers(text: str) -> tuple[list[int], list[int]]:
    """'12 13 23 25 29 07 10' -> ([12,13,23,25,29], [7,10])."""
    nums = [int(x) for x in text.split()]
    if len(nums) != 7:
        raise ValueError(f"unexpected draw result: {text!r}")
    return sorted(nums[:5]), sorted(nums[5:])


def fmt(front, back) -> str:
    return " ".join(f"{n:02d}" for n in sorted(front)) + " + " + " ".join(f"{n:02d}" for n in sorted(back))


def num(value) -> float | None:
    """'750,797,513.86' -> 750797513.86; '---' -> None."""
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text or text in {"---", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ----------------------------------------------------------------------------- 抓取
def fetch_page(page: int, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(API.format(page=page), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not payload.get("success"):
        raise RuntimeError(f"official API error: {payload.get('errorMessage')}")
    return payload["value"]


def store_draw(conn: sqlite3.Connection, item: dict) -> bool:
    draw_no = str(item["lotteryDrawNum"])
    result = item.get("lotteryDrawResult") or ""
    if not result:
        return False
    front, back = parse_numbers(result)
    conn.execute(
        "INSERT OR REPLACE INTO draws (draw_no, draw_date, front, back, pool, sales, raw_time) VALUES (?,?,?,?,?,?,?)",
        (draw_no, item["lotteryDrawTime"], " ".join(map(str, front)), " ".join(map(str, back)),
         num(item.get("poolBalance")), num(item.get("totalSaleAmount")), item.get("lotteryDrawTime")),
    )
    for prize in item.get("prizeLevelList") or []:
        conn.execute(
            "INSERT OR REPLACE INTO prizes (draw_no, level, count, amount) VALUES (?,?,?,?)",
            (draw_no, prize.get("prizeLevel"), prize.get("stakeCount"), num(prize.get("stakeAmount"))),
        )
    return True


def fetch(full: bool = False) -> dict:
    """Incremental: page 1 newest first; stop once a page holds only known draws (unless full)."""
    conn = connect()
    known = {row["draw_no"] for row in conn.execute("SELECT draw_no FROM draws")}
    first = fetch_page(1)
    pages = int(first.get("pages") or 1)
    stored = 0
    stop = False
    for page in range(1, pages + 1):
        value = first if page == 1 else fetch_page(page)
        items = value.get("list") or []
        new_on_page = 0
        for item in items:
            if store_draw(conn, item):
                stored += 1
                if str(item["lotteryDrawNum"]) not in known:
                    new_on_page += 1
        conn.commit()
        if not full and page > 1 and new_on_page == 0:
            stop = True
            break
    latest = conn.execute("SELECT draw_no, draw_date FROM draws ORDER BY draw_no DESC LIMIT 1").fetchone()
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_fetch', ?)",
                 (now().strftime("%Y-%m-%d %H:%M:%S"),))
    conn.commit()
    conn.close()
    return {
        "total_in_db": len(known) + stored if not stop else None,
        "stored": stored,
        "stopped_early": stop,
        "latest": dict(latest) if latest else None,
    }


# ----------------------------------------------------------------------------- 日程
def next_draw(conn: sqlite3.Connection) -> dict | None:
    """下一期：期号 = 最新期号 + 1（同一年内顺序号加一），日期取下一个开奖日。"""
    row = conn.execute("SELECT draw_no, draw_date FROM draws ORDER BY draw_no DESC LIMIT 1").fetchone()
    if not row:
        return None
    try:
        latest_no = int(row["draw_no"])
    except ValueError:
        return None
    moment = now()
    today = moment.date()
    draw_moment = datetime.combine(today, datetime.min.time()).replace(hour=DRAW_TIME[0], minute=DRAW_TIME[1])
    candidate = today
    if today.weekday() in DRAW_WEEKDAYS and moment < draw_moment:
        candidate = today
    else:
        for offset in range(1, 8):
            probe = today + timedelta(days=offset)
            if probe.weekday() in DRAW_WEEKDAYS:
                candidate = probe
                break
    # 若当天还没开奖，下一期就是当期（最新 + 1）；开奖后则再 + 1。
    seq = latest_no + 1
    return {"draw_no": str(seq), "draw_date": candidate.isoformat(),
            "weekday": "一二三四五六日"[candidate.weekday()],
            "is_today": candidate == today}


def is_draw_day(day: date | None = None) -> bool:
    return (day or now().date()).weekday() in DRAW_WEEKDAYS


# ----------------------------------------------------------------------------- 对奖
def judge(front_hit: int, back_hit: int) -> str | None:
    """现行 7 奖级（2026-02-02 第 26014 期起，13 个中奖条件）。"""
    if front_hit == 5 and back_hit == 2:
        return "一等奖"
    if front_hit == 5 and back_hit == 1:
        return "二等奖"
    if (front_hit == 5 and back_hit == 0) or (front_hit == 4 and back_hit == 2):
        return "三等奖"
    if front_hit == 4 and back_hit == 1:
        return "四等奖"
    if (front_hit == 4 and back_hit == 0) or (front_hit == 3 and back_hit == 2):
        return "五等奖"
    if (front_hit == 3 and back_hit == 1) or (front_hit == 2 and back_hit == 2):
        return "六等奖"
    if (front_hit == 3 and back_hit == 0) or (front_hit == 2 and back_hit == 1) \
            or (front_hit == 1 and back_hit == 2) or (front_hit == 0 and back_hit == 2):
        return "七等奖"
    return None


def prize_amount(conn: sqlite3.Connection, draw_no: str, level: str, add_flag: bool) -> float:
    """金额一律取官方当期实发值；只有追加票的一、二等奖按官方"追加"档位另取。"""
    def amount_of(name: str) -> float:
        row = conn.execute("SELECT amount FROM prizes WHERE draw_no=? AND level=?", (draw_no, name)).fetchone()
        return float(row["amount"]) if row and row["amount"] is not None else 0.0

    base = amount_of(level)
    if add_flag and level in FLOATING:
        extra = amount_of(f"{level}(追加)")
        return base + (extra if extra else round(base * 0.8))
    return base


def hit(front_ticket, back_ticket, front_draw, back_draw) -> tuple[int, int]:
    return (len(set(front_ticket) & set(front_draw)), len(set(back_ticket) & set(back_draw)))


# ----------------------------------------------------------------------------- 生成
def gen_uniform() -> tuple[list[int], list[int]]:
    return sorted(random.sample(range(FRONT_MIN, FRONT_MAX + 1), FRONT_COUNT)), sorted(random.sample(range(BACK_MIN, BACK_MAX + 1), BACK_COUNT))


def gen_balanced() -> tuple[list[int], list[int]]:
    """结构均衡偏好：和值 75~115、奇偶 2:3/3:2、大小 2:3/3:2、三个区间各至少 1 个、后区一奇一偶。

    只改变号码的"形状"，**不改变中奖概率**。
    """
    for _ in range(4000):
        front, back = gen_uniform()
        if not 75 <= sum(front) <= 115:
            continue
        if sum(x % 2 for x in front) not in (2, 3):
            continue
        if sum(x >= 18 for x in front) not in (2, 3):
            continue
        zones = [sum(1 for x in front if lo <= x <= hi) for lo, hi in ((1, 12), (13, 24), (25, 35))]
        if 0 in zones:
            continue
        if back[0] % 2 == back[1] % 2:
            continue
        return front, back
    return gen_uniform()


def gen_hot_cold(conn: sqlite3.Connection, window: int = HOT_WINDOW) -> tuple[list[int], list[int]]:
    """冷热偏好：近 N 期出现次数排序，取 3 热 + 2 冷（后区 1 热 + 1 冷）。仅提供"研究感"。"""
    rows = conn.execute("SELECT front, back FROM draws ORDER BY draw_no DESC LIMIT ?", (window,)).fetchall()
    if not rows:
        return gen_uniform()
    front_count = {n: 0 for n in range(FRONT_MIN, FRONT_MAX + 1)}
    back_count = {n: 0 for n in range(BACK_MIN, BACK_MAX + 1)}
    for row in rows:
        for n in map(int, row["front"].split()):
            front_count[n] += 1
        for n in map(int, row["back"].split()):
            back_count[n] += 1
    hot_front = sorted(front_count, key=lambda n: (-front_count[n], n))[:12]
    cold_front = sorted(front_count, key=lambda n: (front_count[n], n))[:12]
    hot_back = sorted(back_count, key=lambda n: (-back_count[n], n))[:5]
    cold_back = sorted(back_count, key=lambda n: (back_count[n], n))[:5]
    front = sorted(random.sample(hot_front, min(3, len(hot_front))) + random.sample(cold_front, 2))
    while len(set(front)) < 5:  # 冷热池可能重叠
        front = sorted(set(front) | set(random.sample(range(FRONT_MIN, FRONT_MAX + 1), 5 - len(set(front)))))
    back = sorted({random.choice(hot_back), random.choice(cold_back)})
    while len(back) < 2:
        back = sorted(set(random.sample(range(BACK_MIN, BACK_MAX + 1), BACK_COUNT)))
    return front, back


def balanced_ok(front: list[int], back: list[int]) -> bool:
    """结构均衡约束：和值 75~115、奇偶 2:3/3:2、大小 2:3/3:2、三区间各≥1、后区一奇一偶。"""
    return (75 <= sum(front) <= 115
            and sum(x % 2 for x in front) in (2, 3)
            and sum(x >= 18 for x in front) in (2, 3)
            and all(any(lo <= x <= hi for x in front) for lo, hi in ((1, 12), (13, 24), (25, 35)))
            and back[0] % 2 != back[1] % 2)


def unpopular_ok(front: list[int], previous: set[int]) -> bool:
    """避开大众组合：不与上一期重号、不含三连号。

    这**不提高中奖率**，只在"万一中头奖"时降低与他人平分的概率（一、二等奖按注均分）。
    """
    if set(front) & previous:
        return False
    return not any(front[i + 2] - front[i] == 2 for i in range(3))


def make_tickets(conn: sqlite3.Connection, n: int = 3) -> list[dict]:
    """3 注，并且故意留一个对照组：

    第 1 注 `uniform`  纯随机，**不经过任何滤网**——它是对照组，用来检验"偏好策略是否真的更好"。
    第 2 注 `balanced` 结构均衡偏好（和值/奇偶/大小/分区/后区奇偶）。
    第 3 注 `hot-cold` 近 30 期冷热偏好。

    第 2、3 注共用一组硬约束：不与上期重号、不含三连号、三个区间各至少 1 个、注间前区重叠≤1。
    这些滤网只改变"买哪一注"，**不改变任何一注的中奖概率**——对照组的存在就是为了证明这一点。
    """
    last = conn.execute("SELECT front FROM draws ORDER BY draw_no DESC LIMIT 1").fetchone()
    previous = set(map(int, last["front"].split())) if last else set()
    plans = [("uniform", gen_uniform), ("balanced", gen_balanced),
             (f"hot-cold({HOT_WINDOW})", lambda: gen_hot_cold(conn))]
    tickets: list[dict] = []
    for index in range(n):
        strategy, generator = plans[index % len(plans)]
        if strategy == "uniform":
            # 对照组：不加任何滤网，保留真正的均匀分布。
            front, back = generator()
        else:
            pick = None
            for _ in range(4000):
                front, back = generator()
                zones = [sum(1 for x in front if lo <= x <= hi) for lo, hi in ((1, 12), (13, 24), (25, 35))]
                if 0 in zones:
                    continue
                if strategy == "balanced" and not balanced_ok(front, back):
                    continue
                if not unpopular_ok(front, previous):
                    continue
                if any(len(set(front) & set(existing["front"])) > 1 for existing in tickets):
                    continue
                pick = (front, back)
                break
            if pick is None:  # 极端情况下退化为随机，保证一定出票
                pick = gen_uniform()
            front, back = pick
        tickets.append({"seq": index + 1, "front": front, "back": back, "strategy": strategy,
                        "add_flag": 0, "cost": PRICE_BASIC})
    return tickets


def gen_compound(conn: sqlite3.Connection, front_n: int = 7, back_n: int = 2) -> dict:
    """复式选号：定胆 + 杀号 + 分区均衡，产出一个"号码池"。

    这里用的是彩民圈那套话术，但实现上只是选号偏好，**不改变任何一注的中奖概率**：
    - 定胆：近 HOT_WINDOW 期出现次数最多的 2 个前区号直接进池；
    - 杀号：全局最冷的 2 个号排除；
    - 分区：前区按三个区间均衡分配，保证每区都有号；
    - 后区：1 个热号 + 1 个冷号，不够再随机补。
    """
    window = conn.execute("SELECT front, back FROM draws ORDER BY draw_no DESC LIMIT ?",
                          (HOT_WINDOW,)).fetchall()
    front_count = {n: 0 for n in range(FRONT_MIN, FRONT_MAX + 1)}
    back_count = {n: 0 for n in range(BACK_MIN, BACK_MAX + 1)}
    for row in window:
        for number in map(int, row["front"].split()):
            front_count[number] += 1
        for number in map(int, row["back"].split()):
            back_count[number] += 1
    hot_front = sorted(front_count, key=lambda n: (-front_count[n], n))
    cold_front = sorted(front_count, key=lambda n: (front_count[n], n))
    hot_back = sorted(back_count, key=lambda n: (-back_count[n], n))
    cold_back = sorted(back_count, key=lambda n: (back_count[n], n))

    anchors = hot_front[:2]                     # 定胆
    banned = set(cold_front[:2])                # 杀号
    zones = ((1, 12), (13, 24), (25, 35))
    base, extra = divmod(front_n, 3)
    allocation = [base + (1 if index < extra else 0) for index in range(3)]
    front: list[int] = list(anchors)
    for (low, high), need in zip(zones, allocation):
        inside = sum(1 for number in front if low <= number <= high)
        want = max(0, need - inside)
        pool = [n for n in range(low, high + 1) if n not in banned and n not in front]
        front += random.sample(pool, min(want, len(pool)))
    pool = [n for n in range(FRONT_MIN, FRONT_MAX + 1) if n not in banned and n not in front]
    if len(front) < front_n:
        front += random.sample(pool, min(front_n - len(front), len(pool)))
    front = sorted(front)[:front_n]

    back = [hot_back[0], cold_back[0]] if back_n >= 2 else [hot_back[0]]
    if len(back) < back_n:
        rest = [n for n in range(BACK_MIN, BACK_MAX + 1) if n not in back]
        back += random.sample(rest, min(back_n - len(back), len(rest)))
    back = sorted(set(back))[:back_n]

    plan = compound_plan(front_n, back_n)
    return {"seq": 1, "front": front, "back": back, "kind": "compound",
            "strategy": f"compound({front_n}+{back_n})", "bets": plan["bets"],
            "add_flag": 0, "cost": plan["cost"]}


# ----------------------------------------------------------------------------- 确定性选号（默认方案）
# 权重集中在这里，改它就是"调策略"；因为没有 random，结果永远可复现、可逐项复核。
SCORE_WEIGHTS = {"heat": 0.55, "gap": 0.45}


def number_stats(conn: sqlite3.Connection, window: int = HOT_WINDOW) -> dict:
    """近 window 期的出现次数、距上次开出的期数、以及上一期号码。"""
    rows = conn.execute("SELECT front, back FROM draws ORDER BY draw_no DESC LIMIT ?",
                        (window,)).fetchall()
    front_freq = {n: 0 for n in range(FRONT_MIN, FRONT_MAX + 1)}
    back_freq = {n: 0 for n in range(BACK_MIN, BACK_MAX + 1)}
    front_gap = {n: len(rows) for n in range(FRONT_MIN, FRONT_MAX + 1)}
    back_gap = {n: len(rows) for n in range(BACK_MIN, BACK_MAX + 1)}
    for index, row in enumerate(rows):
        for number in map(int, row["front"].split()):
            front_freq[number] += 1
            if front_gap[number] == len(rows):
                front_gap[number] = index
        for number in map(int, row["back"].split()):
            back_freq[number] += 1
            if back_gap[number] == len(rows):
                back_gap[number] = index
    last = rows[0] if rows else None
    return {"window": len(rows), "front_freq": front_freq, "back_freq": back_freq,
            "front_gap": front_gap, "back_gap": back_gap,
            "last_front": set(map(int, last["front"].split())) if last else set(),
            "last_back": set(map(int, last["back"].split())) if last else set()}


def score_numbers(freq: dict[int, int], gap: dict[int, int], last: set[int]) -> dict[int, float]:
    """确定性评分 = 0.55×热度 + 0.45×回补；与上期重号的乘 0.6。

    这两个因子只是对历史的描述，**分数高不代表更容易开出**。它唯一的优点是：
    同样的数据永远得到同样的号码（没有随机），且每一项都能复核。
    """
    top = max(freq.values()) or 1
    longest = max(gap.values()) or 1
    scores: dict[int, float] = {}
    for number in freq:
        heat = freq[number] / top
        back = gap[number] / longest
        score = SCORE_WEIGHTS["heat"] * heat + SCORE_WEIGHTS["gap"] * back
        if number in last:
            score *= 0.6
        scores[number] = round(score, 6)
    return scores


def pick_by_zone(scores: dict[int, float], count: int,
                 zones=((1, 12), (13, 24), (25, 35))) -> list[int]:
    """按区间配额确定性挑选：分高的优先，同分按号码升序。

    额外一条确定性去连号规则：优先挑与已选号码不相邻的号（池里连号太多会让票面很"扎堆"）；
    若按该规则凑不够，第二轮再放开限制补齐。全程没有随机数。
    """
    base, extra = divmod(count, len(zones))
    quota = [base + (1 if index < extra else 0) for index in range(len(zones))]
    chosen: list[int] = []

    def take(low: int, high: int, need: int, avoid_adjacent: bool) -> int:
        picked = 0
        ranked = sorted((n for n in range(low, high + 1) if n in scores and n not in chosen),
                        key=lambda n: (-scores[n], n))
        for number in ranked:
            if picked >= need:
                break
            if avoid_adjacent and any(abs(number - other) == 1 for other in chosen):
                continue
            chosen.append(number)
            picked += 1
        return picked

    for (low, high), need in zip(zones, quota):
        take(low, high, need, True)
        inside = sum(1 for number in chosen if low <= number <= high)
        if inside < need:
            take(low, high, need - inside, False)
    if len(chosen) < count:
        rest = sorted((n for n in scores if n not in chosen), key=lambda n: (-scores[n], n))
        chosen += rest[:count - len(chosen)]
    return sorted(chosen[:count])


def gen_optimized(conn: sqlite3.Connection, front_n: int = TICKET_FRONT_N,
                  back_n: int = TICKET_BACK_N, add: bool = TICKET_ADD) -> dict:
    """每期一注的确定性复式：同样的数据 → 同样的号码，没有 random。"""
    stats = number_stats(conn)
    front_scores = score_numbers(stats["front_freq"], stats["front_gap"], stats["last_front"])
    back_scores = score_numbers(stats["back_freq"], stats["back_gap"], stats["last_back"])
    front = pick_by_zone(front_scores, front_n)
    ranked = sorted(back_scores, key=lambda n: (-back_scores[n], n))
    back_set = {ranked[0], ranked[-1]}          # 一热一冷
    for number in ranked:
        if len(back_set) >= back_n:
            break
        back_set.add(number)
    back = sorted(back_set)[:back_n]
    plan = compound_plan(front_n, back_n, add)
    return {"seq": 1, "front": front, "back": back, "kind": "compound",
            "strategy": f"deterministic({front_n}+{back_n}{'+追加' if add else ''})",
            "bets": plan["bets"], "add_flag": 1 if add else 0, "cost": plan["cost"],
            "scores": {"window": stats["window"], "weights": SCORE_WEIGHTS,
                       "front": {str(n): front_scores[n] for n in front},
                       "back": {str(n): back_scores[n] for n in back}}}


def predict(draw_no: str | None = None, force: bool = False, compound: str | None = None,
            singles: bool = False, add: bool | None = None,
            front_n: int | None = None, back_n: int | None = None) -> dict:
    conn = connect()
    target = next_draw(conn)
    if not target:
        conn.close()
        return {"ok": False, "error": "数据库为空，先运行 fetch"}
    if draw_no:
        target["draw_no"] = draw_no
    use_add = TICKET_ADD if add is None else bool(add)
    # 默认：每期一注 6+2 复式（带追加）；--singles 才回到 3 注单式；--compound 可指定池大小。
    if compound:
        try:
            pool_front, pool_back = (int(part) for part in compound.replace("x", "+").split("+"))
        except ValueError:
            conn.close()
            return {"ok": False, "error": f"复式参数看不懂：{compound}，应写成 7+2 这样"}
        kind, front_n, back_n = "compound", pool_front, pool_back
    elif singles:
        kind = "single"
    else:
        kind = "compound"
        front_n = front_n or TICKET_FRONT_N
        back_n = back_n or TICKET_BACK_N
    existing = conn.execute("SELECT COUNT(*) AS c FROM tickets WHERE draw_no=? AND kind=?",
                            (target["draw_no"], kind)).fetchone()["c"]
    if existing and not force:
        conn.close()
        return {"ok": True, "skipped": True,
                "reason": f"第 {target['draw_no']} 期已有 {existing} 注{kind}",
                "target": target}
    if existing and force:
        conn.execute("DELETE FROM tickets WHERE draw_no=? AND kind=?", (target["draw_no"], kind))

    tickets: list[dict] = []
    if kind == "single":
        tickets = make_tickets(conn)
    else:
        try:
            tickets = [gen_optimized(conn, front_n, back_n, use_add)]
        except ValueError as exc:
            conn.close()
            return {"ok": False, "error": str(exc)}
    if kind == "compound":
        # 同期的单式可能已经占了 1..n，复式接着编号
        start = conn.execute("SELECT COALESCE(MAX(seq),0) AS s FROM tickets WHERE draw_no=?",
                             (target["draw_no"],)).fetchone()["s"]
        tickets[0]["seq"] = int(start) + 1

    created = now().strftime("%Y-%m-%d %H:%M:%S")
    for ticket in tickets:
        conn.execute(
            "INSERT INTO tickets (draw_no, seq, front, back, strategy, add_flag, cost, created_at, kind, bets)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (target["draw_no"], ticket["seq"], " ".join(f"{n:02d}" for n in ticket["front"]),
             " ".join(f"{n:02d}" for n in ticket["back"]), ticket["strategy"], ticket["add_flag"],
             ticket["cost"], created, ticket.get("kind", "single"), ticket.get("bets", 1)))
    conn.commit()
    rows = conn.execute("SELECT * FROM tickets WHERE draw_no=? ORDER BY seq", (target["draw_no"],)).fetchall()
    conn.close()
    return {"ok": True, "target": target, "tickets": [dict(r) for r in rows],
            "cost_total": sum(r["cost"] for r in rows)}


# ----------------------------------------------------------------------------- 对奖执行
def settle_compound(conn: sqlite3.Connection, front_pool: list[int], back_pool: list[int],
                    draw_front: list[int], draw_back: list[int], draw_no: str,
                    add_flag: bool) -> tuple[tuple[int, int], str | None, float, dict[str, int]]:
    """复式对奖：把号码池展开成每一注分别判奖，再求和。

    复式不是"一注更大的票"，而是 C(前区数,5)×C(后区数,2) 注单式的组合；奖金是这些注的合计。
    """
    total = 0.0
    counts: dict[str, int] = {}
    best_level: str | None = None
    best_hits = (0, 0)
    for front in itertools.combinations(front_pool, FRONT_COUNT):
        for back in itertools.combinations(back_pool, BACK_COUNT):
            front_hit, back_hit = hit(list(front), list(back), draw_front, draw_back)
            if (front_hit, back_hit) > best_hits:
                best_hits = (front_hit, back_hit)
            level = judge(front_hit, back_hit)
            if not level:
                continue
            total += prize_amount(conn, draw_no, level, add_flag)
            counts[level] = counts.get(level, 0) + 1
            if best_level is None or PRIZE_RANK[level] < PRIZE_RANK[best_level]:
                best_level = level
    return best_hits, best_level, total, counts


def check() -> dict:
    conn = connect()
    rows = conn.execute(
        "SELECT t.*, d.front AS draw_front, d.back AS draw_back FROM tickets t"
        " JOIN draws d ON d.draw_no = t.draw_no"
        " LEFT JOIN results r ON r.ticket_id = t.id WHERE r.ticket_id IS NULL ORDER BY t.draw_no, t.seq"
    ).fetchall()
    settled = []
    for row in rows:
        draw_front = [int(x) for x in row["draw_front"].split()]
        draw_back = [int(x) for x in row["draw_back"].split()]
        front_pool = [int(x) for x in row["front"].split()]
        back_pool = [int(x) for x in row["back"].split()]
        kind = row["kind"] or "single"
        if kind == "compound":
            (front_hit, back_hit), level, amount, counts = settle_compound(
                conn, front_pool, back_pool, draw_front, draw_back, row["draw_no"], bool(row["add_flag"]))
            detail = json.dumps({"bets": row["bets"], "levels": counts,
                                 "note": "复式：每注分别计奖后合计"}, ensure_ascii=False)
        else:
            front_hit, back_hit = hit(front_pool, back_pool, draw_front, draw_back)
            level = judge(front_hit, back_hit)
            amount = prize_amount(conn, row["draw_no"], level, bool(row["add_flag"])) if level else 0.0
            counts = {level: 1} if level else {}
            detail = None
        conn.execute(
            "INSERT OR REPLACE INTO results (ticket_id, draw_no, front_hit, back_hit, level, amount, checked_at, detail)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (row["id"], row["draw_no"], front_hit, back_hit, level, amount,
             now().strftime("%Y-%m-%d %H:%M:%S"), detail))
        settled.append({"draw_no": row["draw_no"], "seq": row["seq"], "kind": kind, "bets": row["bets"],
                        "ticket": fmt_str(row["front"], row["back"]),
                        "front_hit": front_hit, "back_hit": back_hit, "level": level,
                        "amount": amount, "levels": counts, "cost": row["cost"]})
    conn.commit()
    conn.close()
    return {"ok": True, "settled": settled, "count": len(settled)}


def fmt_str(front_text: str, back_text: str) -> str:
    return " ".join(f"{int(x):02d}" for x in front_text.split()) + " + " \
           + " ".join(f"{int(x):02d}" for x in back_text.split())


# ----------------------------------------------------------------------------- 报告
def report() -> dict:
    conn = connect()
    spend = conn.execute("SELECT COALESCE(SUM(cost),0) AS s FROM tickets").fetchone()["s"]
    won = conn.execute("SELECT COALESCE(SUM(amount),0) AS s FROM results").fetchone()["s"]
    levels = conn.execute("SELECT COALESCE(level,'未中奖') AS level, COUNT(*) AS c FROM results GROUP BY level").fetchall()
    strategy = conn.execute(
        "SELECT t.strategy, COUNT(*) AS n, AVG(r.front_hit) AS front, AVG(r.back_hit) AS back,"
        " AVG(CASE WHEN r.level IS NULL THEN 0 ELSE 1 END) AS win_rate, SUM(r.amount) AS won"
        " FROM tickets t JOIN results r ON r.ticket_id = t.id GROUP BY t.strategy ORDER BY t.strategy").fetchall()
    latest = conn.execute("SELECT * FROM draws ORDER BY draw_no DESC LIMIT 1").fetchone()
    target = next_draw(conn)
    pending = conn.execute(
        "SELECT t.draw_no, COUNT(*) AS c, SUM(t.cost) AS cost FROM tickets t"
        " LEFT JOIN results r ON r.ticket_id = t.id WHERE r.ticket_id IS NULL GROUP BY t.draw_no"
    ).fetchall()
    conn.close()
    roi = (won / spend) if spend else 0.0
    return {
        "spend": spend, "won": won, "roi": round(roi, 4),
        "theoretical_return": 0.51,
        "tickets": sum(int(r["c"]) for r in levels) or 0,
        "levels": [dict(r) for r in levels],
        "strategies": [{"strategy": r["strategy"], "tickets": r["n"],
                        "avg_front_hit": round(r["front"], 3), "avg_back_hit": round(r["back"], 3),
                        "expected_front_hit": round(5 * 5 / 35, 3), "expected_back_hit": round(2 * 2 / 12, 3),
                        "win_rate": round(r["win_rate"], 4), "expected_win_rate": 0.0667,
                        "won": r["won"]} for r in strategy],
        "latest_draw": dict(latest) if latest else None,
        "next_draw": target,
        "pending": [dict(r) for r in pending],
    }


def selftest() -> dict:
    """官方 26125 期（12 13 23 25 29 + 07 10）的 11 个用例。"""
    draw = ([12, 13, 23, 25, 29], [7, 10])
    cases = [
        (([12, 13, 23, 25, 29], [7, 10]), "一等奖"),
        (([12, 13, 23, 25, 29], [7, 11]), "二等奖"),
        (([12, 13, 23, 25, 29], [1, 2]), "三等奖"),
        (([12, 13, 23, 25, 30], [7, 10]), "三等奖"),
        (([12, 13, 23, 25, 30], [7, 11]), "四等奖"),
        (([12, 13, 23, 24, 30], [1, 2]), "七等奖"),
        (([12, 13, 23, 24, 30], [7, 11]), "六等奖"),
        (([12, 13, 22, 24, 30], [7, 10]), "六等奖"),
        (([12, 13, 22, 24, 31], [7, 12]), "七等奖"),
        (([1, 2, 3, 4, 5], [7, 10]), "七等奖"),
        (([1, 2, 3, 4, 5], [1, 2]), None),
    ]
    failures = []
    for (front, back), expected in cases:
        got = judge(*hit(front, back, *draw))
        if got != expected:
            failures.append({"ticket": fmt(front, back), "expected": expected, "got": got})
    return {"ok": not failures, "cases": len(cases), "failures": failures}


# ----------------------------------------------------------------------------- CLI
def status() -> dict:
    conn = connect()
    latest = conn.execute("SELECT * FROM draws ORDER BY draw_no DESC LIMIT 1").fetchone()
    target = next_draw(conn)
    tickets = []
    if target:
        tickets = [dict(r) for r in conn.execute(
            "SELECT * FROM tickets WHERE draw_no=? ORDER BY seq", (target["draw_no"],))]
    last_results = [dict(r) for r in conn.execute(
        "SELECT r.*, t.front, t.back, t.strategy FROM results r JOIN tickets t ON t.id=r.ticket_id"
        " ORDER BY r.checked_at DESC, r.ticket_id DESC LIMIT 6")]
    conn.close()
    return {"latest_draw": dict(latest) if latest else None, "next_draw": target,
            "prediction": tickets, "last_results": last_results,
            "rules_check": rules_summary(), "optimize_check": optimize_summary(),
            "report": report()}


def optimize_summary() -> dict | None:
    """读 optimize.py 留下的最近一次评估结果（不联网，纯读文件）。"""
    path = os.path.join(BASE, "optimize-report.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (ValueError, OSError):
        return None
    best = data.get("best_in_sample") or {}
    oos = data.get("out_of_sample") or {}
    return {"at": data.get("at"), "significant": data.get("significant"),
            "best_in_sample": best, "threshold": data.get("threshold"),
            "out_of_sample": {"avg_front_hit": oos.get("avg_front_hit"), "z": oos.get("z")}}


def rules_summary() -> dict | None:
    """读 crawler.py 留下的最近一次规则比对结果（不联网，纯读文件）。"""
    path = os.path.join(BASE, "rules.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (ValueError, OSError):
        return None
    mismatches = [c for c in data.get("checks", []) if c.get("status") == "MISMATCH"]
    return {"verdict": data.get("verdict"), "fetched_at": data.get("fetched_at"),
            "checked": len(data.get("checks", [])), "mismatches": mismatches}


def daily() -> dict:
    out: dict = {"at": now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        out["fetch"] = fetch()
    except Exception as exc:  # noqa: BLE001 - scheduled run must not die on network
        out["fetch_error"] = f"{type(exc).__name__}: {exc}"
    out["check"] = check()
    if is_draw_day():
        try:
            out["predict"] = predict()
        except Exception as exc:  # noqa: BLE001
            out["predict_error"] = f"{type(exc).__name__}: {exc}"
    out["report"] = report()
    return out


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "status"
    if command == "fetch":
        print(json.dumps(fetch(full="--full" in argv), ensure_ascii=False, indent=2))
    elif command == "predict":
        options: dict = {"force": "--force" in argv, "singles": "--singles" in argv}
        for index, arg in enumerate(argv):
            if arg == "--compound" and index + 1 < len(argv):
                options["compound"] = argv[index + 1]
            elif arg.startswith("--compound="):
                options["compound"] = arg.split("=", 1)[1]
            elif arg in ("--front", "--back") and index + 1 < len(argv):
                options[arg[2:] + "_n"] = int(argv[index + 1])
            elif arg in ("--add", "--no-add"):
                options["add"] = arg == "--add"
        print(json.dumps(predict(**options), ensure_ascii=False, indent=2))
    elif command == "plans":
        rows = []
        for front, back in ((6, 2), (6, 3), (7, 2), (7, 3), (8, 2), (8, 3)):
            for add in (False, True):
                rows.append(compound_plan(front, back, add))
        print(json.dumps({row["label"]: row for row in rows}, ensure_ascii=False, indent=2))
    elif command == "check":
        print(json.dumps(check(), ensure_ascii=False, indent=2))
    elif command == "report":
        print(json.dumps(report(), ensure_ascii=False, indent=2))
    elif command == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif command == "selftest":
        result = selftest()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    elif command == "daily":
        print(json.dumps(daily(), ensure_ascii=False, indent=2))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
