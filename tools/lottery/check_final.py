"""Final check of the one-ticket-per-draw configuration.

Verifies:
  - the draw has exactly one ticket, and it is the deterministic compound with add-on;
  - the no-add variant costs exactly 12 yuan for the same 6+2 pool;
  - the stored ticket is reproducible: recomputing gen_optimized gives the same numbers.
Run: python check_final.py
"""
import sys
import lottery  # noqa: E402

conn = lottery.connect()
target = lottery.next_draw(conn)
rows = conn.execute("SELECT * FROM tickets WHERE draw_no=? ORDER BY seq", (target["draw_no"],)).fetchall()
print(f"draw {target['draw_no']} ({target['draw_date']}) has {len(rows)} ticket(s)")
for row in rows:
    print(f"  seq={row['seq']} {row['front']} + {row['back']}")
    print(f"      kind={row['kind']} strategy={row['strategy']} bets={row['bets']}"
          f" add={bool(row['add_flag'])} cost={row['cost']}元")

failures = []
if len(rows) != 1:
    failures.append(f"expected exactly one ticket, found {len(rows)}")
else:
    row = rows[0]
    if row["kind"] != "compound":
        failures.append(f"expected a compound ticket, got {row['kind']}")
    if not row["add_flag"]:
        failures.append("expected the add-on to be on")
    if row["cost"] != 18:
        failures.append(f"expected 18 yuan, got {row['cost']}")
    stored_front = [int(x) for x in row["front"].split()]
    stored_back = [int(x) for x in row["back"].split()]
    again = lottery.gen_optimized(conn, 6, 2, True)
    if again["front"] != stored_front or again["back"] != stored_back:
        failures.append(f"not reproducible: stored {stored_front}/{stored_back}"
                        f" vs recomputed {again['front']}/{again['back']}")
    else:
        print("  reproducible: recomputation matches the stored numbers")

no_add = lottery.gen_optimized(conn, 6, 2, False)
print(f"no-add variant: {no_add['cost']} yuan / {no_add['bets']} bets (expected 12 / 6)")
if no_add["cost"] != 12 or no_add["bets"] != 6:
    failures.append("no-add variant arithmetic wrong")

conn.close()
if failures:
    print("FAIL:")
    for item in failures:
        print("  -", item)
    raise SystemExit(1)
print("OK: one deterministic add-on compound ticket per draw, verified")
