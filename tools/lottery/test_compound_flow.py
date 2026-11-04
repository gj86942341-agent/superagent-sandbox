"""End-to-end test of compound (复式) settlement on a real drawn period.

Draw 26125 = 12 13 23 25 29 + 07 10.
A 7+3 compound ticket covering all winning numbers must settle as:
  - exactly one 一等奖 (the only front combination equal to the 5 winners × the only back pair),
  - plus a predictable set of smaller prizes,
  - with total prize = the independent sum over all 63 single bets.

Test rows are removed afterwards; the real record is left untouched.
Run: python test_compound_flow.py
"""
import itertools
import sys
import lottery  # noqa: E402

DRAW = "26125"
FRONT_POOL = [12, 13, 23, 24, 25, 29, 30]
BACK_POOL = [7, 10, 11]


def main() -> int:
    conn = lottery.connect()
    draw = conn.execute("SELECT * FROM draws WHERE draw_no=?", (DRAW,)).fetchone()
    if not draw:
        print(f"draw {DRAW} missing; run fetch first")
        return 1
    draw_front = [int(x) for x in draw["front"].split()]
    draw_back = [int(x) for x in draw["back"].split()]
    print(f"draw {DRAW}: {draw['front']} + {draw['back']}")

    plan = lottery.compound_plan(7, 3)
    print(f"compound 7+3 -> {plan['bets']} bets, {plan['cost']} yuan")

    cursor = conn.execute(
        "INSERT INTO tickets (draw_no, seq, front, back, strategy, add_flag, cost, created_at, kind, bets)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (DRAW, 998, " ".join(f"{n:02d}" for n in FRONT_POOL), " ".join(f"{n:02d}" for n in BACK_POOL),
         "compound(7+3)", 0, plan["cost"], "2000-01-01 00:00:00", "compound", plan["bets"]))
    ticket_id = cursor.lastrowid
    conn.commit()
    conn.close()

    settled = [row for row in lottery.check()["settled"]
               if row["draw_no"] == DRAW and row["seq"] == 998]
    if not settled:
        print("FAIL: compound ticket was not settled")
        return 1
    result = settled[0]
    print(f"settled: best {result['front_hit']}+{result['back_hit']} -> {result['level']}, "
          f"total {result['amount']} yuan")
    print(f"prize breakdown: {result['levels']}")

    # Independent recomputation: expand the pool ourselves and sum the prizes.
    expected_total = 0.0
    expected_counts: dict[str, int] = {}
    conn = lottery.connect()
    for front in itertools.combinations(FRONT_POOL, 5):
        for back in itertools.combinations(BACK_POOL, 2):
            front_hit, back_hit = lottery.hit(list(front), list(back), draw_front, draw_back)
            level = lottery.judge(front_hit, back_hit)
            if level:
                expected_total += lottery.prize_amount(conn, DRAW, level, False)
                expected_counts[level] = expected_counts.get(level, 0) + 1
    conn.execute("DELETE FROM results WHERE ticket_id=?", (ticket_id,))
    conn.execute("DELETE FROM tickets WHERE id=?", (ticket_id,))
    conn.commit()
    leftover = conn.execute("SELECT COUNT(*) AS c FROM tickets WHERE strategy LIKE 'compound%'").fetchone()["c"]
    conn.close()

    failures = []
    if result["levels"] != expected_counts:
        failures.append(f"breakdown {result['levels']} != expected {expected_counts}")
    if abs(result["amount"] - expected_total) > 0.01:
        failures.append(f"amount {result['amount']} != expected {expected_total}")
    if expected_counts.get("一等奖") != 1:
        failures.append(f"expected exactly one jackpot, got {expected_counts.get('一等奖')}")
    if result["bets"] != plan["bets"]:
        failures.append(f"bets {result['bets']} != {plan['bets']}")

    print(f"independent recomputation: {expected_counts}, total {expected_total}")
    print(f"cleaned up, compound rows left: {leftover}")
    if failures:
        print("FAIL:")
        for item in failures:
            print("  -", item)
        return 1
    print("OK: compound settlement matches the independent per-bet calculation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
