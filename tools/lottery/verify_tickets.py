"""Verify the tickets generated for the next draw.

Single tickets: 5 front + 2 back, no repeat of the previous draw, no triple, only the
balanced strategy must satisfy the structure preference, and single tickets must not
overlap each other by more than one number.
Compound tickets: a number pool whose size matches its kind, whose bet count and cost
equal C(front,5)×C(back,2) and bets×2.

None of these checks says anything about the chance of winning: they only verify the
declared preferences and the arithmetic.
Run: python verify_tickets.py
"""
import math
import sys
import lottery  # noqa: E402

ZONES = ((1, 12), (13, 24), (25, 35))


def main() -> int:
    conn = lottery.connect()
    latest = conn.execute("SELECT draw_no, front FROM draws ORDER BY draw_no DESC LIMIT 1").fetchone()
    prev = set(map(int, latest["front"].split()))
    target = lottery.next_draw(conn)
    rows = conn.execute("SELECT * FROM tickets WHERE draw_no=? ORDER BY seq", (target["draw_no"],)).fetchall()
    print(f"latest draw {latest['draw_no']} front = {sorted(prev)}")
    print(f"target      {target['draw_no']} on {target['draw_date']} (weekday {target['weekday']})")
    print()

    failures: list[str] = []
    single_fronts: list[set[int]] = []
    for row in rows:
        front = [int(x) for x in row["front"].split()]
        back = [int(x) for x in row["back"].split()]
        kind = row["kind"] or "single"
        if kind == "compound":
            expected_bets = math.comb(len(front), lottery.FRONT_COUNT) * math.comb(len(back), lottery.BACK_COUNT)
            unit = lottery.PRICE_BASIC + (lottery.ADDON_PRICE if row["add_flag"] else 0)
            expected_cost = expected_bets * unit
            print(f"#{row['seq']} [compound] {row['front']} + {row['back']}")
            print(f"     pool={len(front)}+{len(back)}  bets={row['bets']} (expect {expected_bets})"
                  f"  cost={row['cost']} (expect {expected_cost})")
            if row["bets"] != expected_bets:
                failures.append(f"#{row['seq']} bet count {row['bets']} != {expected_bets}")
            if row["cost"] != expected_cost:
                failures.append(f"#{row['seq']} cost {row['cost']} != {expected_cost}")
            if not (lottery.FRONT_COUNT <= len(front) <= 18 and lottery.BACK_COUNT <= len(back) <= 12):
                failures.append(f"#{row['seq']} pool size out of range")
            if len(set(front)) != len(front) or len(set(back)) != len(back):
                failures.append(f"#{row['seq']} duplicate numbers in the pool")
            if any(not 1 <= x <= 35 for x in front) or any(not 1 <= x <= 12 for x in back):
                failures.append(f"#{row['seq']} number out of range")
            continue

        zones = [sum(1 for x in front if lo <= x <= hi) for lo, hi in ZONES]
        overlap_prev = len(set(front) & prev)
        triple = any(front[i + 2] - front[i] == 2 for i in range(3))
        balanced = lottery.balanced_ok(front, back)
        single_fronts.append(set(front))
        print(f"#{row['seq']} [{row['strategy']:<11}] {row['front']} + {row['back']}")
        print(f"     sum={sum(front):3d} odd={sum(x % 2 for x in front)} big={sum(x >= 18 for x in front)}"
              f" zones={zones} overlap_prev={overlap_prev} triple={triple} balanced_ok={balanced}")
        if len(front) != 5 or len(back) != 2 or len(set(front)) != 5 or len(set(back)) != 2:
            failures.append(f"#{row['seq']} wrong shape for a single ticket")
        if overlap_prev:
            failures.append(f"#{row['seq']} repeats last draw numbers")
        if triple:
            failures.append(f"#{row['seq']} contains three consecutive numbers")
        if row["strategy"] == "balanced" and not balanced:
            failures.append(f"#{row['seq']} violates the balanced preference")

    for i in range(len(single_fronts)):
        for j in range(i + 1, len(single_fronts)):
            shared = single_fronts[i] & single_fronts[j]
            if len(shared) > 1:
                failures.append(f"single #{i + 1} and single #{j + 1} share {sorted(shared)}")

    total = sum(row["cost"] for row in rows)
    print()
    print(f"cost for this draw: {total} yuan over {len(rows)} tickets")
    if failures:
        print("FAIL:")
        for item in failures:
            print("  -", item)
        return 1
    print("OK: all declared constraints hold (this says nothing about winning odds)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
