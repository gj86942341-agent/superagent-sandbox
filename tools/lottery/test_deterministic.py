"""Checks for the deterministic one-ticket-per-draw scheme.

1. Determinism: gen_optimized twice on the same database returns the same numbers.
2. Add-on accounting: a compound ticket with 追加 pays base + 80% add-on for a top-tier
   prize, and only the base amount for fixed prizes.
3. Ticket shape: default plan is 6+2 = 6 bets at 3 yuan each = 18 yuan.

Test rows are removed afterwards.
Run: python test_deterministic.py
"""
import sys
import lottery  # noqa: E402

DRAW = "26125"                      # 12 13 23 25 29 + 07 10, already drawn
FRONT_POOL = [12, 13, 23, 25, 29, 30]
BACK_POOL = [7, 10]


def main() -> int:
    failures = []
    conn = lottery.connect()

    # --- 1. determinism -----------------------------------------------------
    first = lottery.gen_optimized(conn, 6, 2, True)
    second = lottery.gen_optimized(conn, 6, 2, True)
    same = first["front"] == second["front"] and first["back"] == second["back"]
    print(f"deterministic pick: {first['front']} + {first['back']}")
    print(f"  repeated call     : {second['front']} + {second['back']}  -> same={same}")
    print(f"  scores: front={first['scores']['front']} back={first['scores']['back']}")
    if not same:
        failures.append("gen_optimized is not deterministic")

    # --- 2. plan arithmetic -------------------------------------------------
    plan = lottery.compound_plan(6, 2, True)
    print(f"plan 6+2 add: bets={plan['bets']} unit={plan['unit']} cost={plan['cost']}")
    if plan["bets"] != 6 or plan["unit"] != 3 or plan["cost"] != 18:
        failures.append(f"plan arithmetic wrong: {plan}")

    # --- 3. add-on prize accounting ----------------------------------------
    cursor = conn.execute(
        "INSERT INTO tickets (draw_no, seq, front, back, strategy, add_flag, cost, created_at, kind, bets)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (DRAW, 997, " ".join(f"{n:02d}" for n in FRONT_POOL), " ".join(f"{n:02d}" for n in BACK_POOL),
         "test-add", 1, plan["cost"], "2000-01-01 00:00:00", "compound", plan["bets"]))
    ticket_id = cursor.lastrowid
    conn.commit()
    conn.close()

    settled = [row for row in lottery.check()["settled"] if row["seq"] == 997 and row["draw_no"] == DRAW]
    conn = lottery.connect()
    if not settled:
        failures.append("add-on compound was not settled")
    else:
        result = settled[0]
        base_jackpot = lottery.prize_amount(conn, DRAW, "一等奖", False)
        add_jackpot = lottery.prize_amount(conn, DRAW, "一等奖", True)
        fixed_add = lottery.prize_amount(conn, DRAW, "七等奖", True)
        fixed_base = lottery.prize_amount(conn, DRAW, "七等奖", False)
        print(f"  一等奖 base={base_jackpot} with-add={add_jackpot}")
        print(f"  七等奖 base={fixed_base} with-add={fixed_add} (fixed prizes must not grow)")
        expected_add = round(base_jackpot * 1.8, 2)
        if abs(add_jackpot - expected_add) > 1:
            failures.append(f"add-on jackpot {add_jackpot} != 1.8 x base {expected_add}")
        if fixed_add != fixed_base:
            failures.append(f"fixed prize changed with add-on: {fixed_base} -> {fixed_add}")
        print(f"  settled best={result['level']} total={result['amount']} levels={result['levels']}")

    conn.execute("DELETE FROM results WHERE ticket_id=?", (ticket_id,))
    conn.execute("DELETE FROM tickets WHERE id=?", (ticket_id,))
    conn.commit()
    conn.close()

    if failures:
        print("FAIL:")
        for item in failures:
            print("  -", item)
        return 1
    print("OK: deterministic, arithmetic and add-on accounting all correct")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
