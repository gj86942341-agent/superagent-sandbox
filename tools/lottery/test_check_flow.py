"""End-to-end test of settle + prize lookup against a real drawn period (26125).

Inserts two synthetic tickets for the already-drawn 26125, settles them, asserts the
level and the official prize amount, then removes the test rows so the real record
stays clean. Run: python test_check_flow.py
"""
import sys
import lottery  # noqa: E402

DRAW = "26125"          # official result 12 13 23 25 29 + 07 10
CASES = [
    # (front, back, expected level, expected amount from the official prize table)
    ("12 13 23 25 29", "07 10", "一等奖", 7558337.0),
    ("12 13 22 24 31", "07 12", "七等奖", 5.0),
    ("01 02 03 04 05", "01 02", None, 0.0),
]


def main() -> int:
    conn = lottery.connect()
    draw = conn.execute("SELECT * FROM draws WHERE draw_no=?", (DRAW,)).fetchone()
    if not draw:
        print(f"draw {DRAW} not in database; run fetch first")
        return 1
    print(f"draw {DRAW}: {draw['front']} + {draw['back']}")

    created = []
    for front, back, _level, _amount in CASES:
        cursor = conn.execute(
            "INSERT INTO tickets (draw_no, seq, front, back, strategy, add_flag, cost, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (DRAW, 999, front, back, "test", 0, 2, "2000-01-01 00:00:00"))
        created.append(cursor.lastrowid)
    conn.commit()
    conn.close()

    settled = {row["ticket"]: row for row in lottery.check()["settled"] if row["draw_no"] == DRAW}

    failures = []
    for front, back, level, amount in CASES:
        key = " ".join(f"{int(x):02d}" for x in front.split()) + " + " + " ".join(f"{int(x):02d}" for x in back.split())
        got = settled.get(key)
        if not got:
            failures.append(f"{key}: not settled")
            continue
        ok = got["level"] == level and abs(got["amount"] - amount) < 0.01
        print(f"  {key}: {got['front_hit']}+{got['back_hit']} -> {got['level']} {got['amount']}"
              f"  expected {level} {amount}  {'OK' if ok else 'MISMATCH'}")
        if not ok:
            failures.append(f"{key}: expected {level}/{amount}, got {got['level']}/{got['amount']}")

    conn = lottery.connect()
    conn.execute("DELETE FROM results WHERE ticket_id IN (%s)" % ",".join("?" * len(created)), created)
    conn.execute("DELETE FROM tickets WHERE id IN (%s)" % ",".join("?" * len(created)), created)
    conn.commit()
    leftover = conn.execute("SELECT COUNT(*) AS c FROM tickets WHERE strategy='test'").fetchone()["c"]
    conn.close()
    print(f"cleaned up: {leftover} test tickets left")

    if failures:
        print("FAIL:")
        for item in failures:
            print("  -", item)
        return 1
    print("OK: settle and prize lookup match the official data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
