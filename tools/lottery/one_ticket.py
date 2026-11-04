"""Keep exactly one ticket for a draw: the deterministic compound one.

Removes every other ticket of the draw (the old single-ticket strategies) and renumbers
the compound to seq 1, so each period has exactly one ticket as requested.
Run: python one_ticket.py [draw_no]
"""
import sys
import lottery  # noqa: E402

conn = lottery.connect()
draw_no = sys.argv[1] if len(sys.argv) > 1 else lottery.next_draw(conn)["draw_no"]

kept = conn.execute(
    "SELECT id, seq FROM tickets WHERE draw_no=? AND kind='compound' ORDER BY id DESC LIMIT 1",
    (draw_no,)).fetchone()
if not kept:
    print(f"no compound ticket for draw {draw_no}; run: predict --force")
    raise SystemExit(1)

removed = conn.execute("DELETE FROM tickets WHERE draw_no=? AND id<>?", (draw_no, kept["id"])).rowcount
conn.execute("UPDATE tickets SET seq=1 WHERE id=?", (kept["id"],))
conn.commit()

print(f"draw {draw_no}: removed {removed} other ticket(s), kept compound as seq 1")
for row in conn.execute("SELECT seq, front, back, strategy, kind, bets, add_flag, cost FROM tickets"
                        " WHERE draw_no=? ORDER BY seq", (draw_no,)):
    print(f"  seq={row['seq']} {row['front']} + {row['back']}")
    print(f"      {row['strategy']} | {row['kind']} | {row['bets']}注 | 追加={bool(row['add_flag'])}"
          f" | {row['cost']}元")
conn.close()
