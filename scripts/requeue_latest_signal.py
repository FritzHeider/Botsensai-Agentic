#!/usr/bin/env python3
import sqlite3

conn = sqlite3.connect("/home/ubuntu/Botsensai/data/botsensai.db")
cur = conn.cursor()
row = cur.execute("SELECT id, symbol, token_key FROM execution_signals WHERE side = 'BUY' ORDER BY id DESC LIMIT 1").fetchone()
if row:
    sig_id, sym, tok = row
    cur.execute("UPDATE execution_signals SET status = 'PENDING', error = NULL WHERE id = ?", (sig_id,))
    conn.commit()
    print(f"Re-queued signal #{sig_id} (${sym}, {tok}) as PENDING for live execution")
else:
    print("No BUY signals found to re-queue")
