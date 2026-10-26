"""One-time database setup: runs schema.sql (tables, procedures, triggers, view, sample data).
WARNING: schema.sql drops and recreates the smart_farmer database."""
import os
import re
import mysql.connector
import env_loader  # noqa: F401  (loads .env)

BASE = os.path.dirname(os.path.abspath(__file__))


def statements(text):
    delim, buf = ";", ""
    for line in text.splitlines():
        t = re.sub(r"\s--\s.*$", "", line).strip()      # drop inline comments
        if t.upper().startswith("DELIMITER "):
            delim = t.split()[1]
            continue
        if not t or t.startswith("--"):
            continue
        buf += t + "\n"
        if delim == ";":                                  # normal mode: split on every semicolon
            parts = buf.split(";")
            for p in parts[:-1]:
                if p.strip():
                    yield p.strip()
            buf = parts[-1]
        elif t.endswith(delim):                           # procedure/trigger mode: custom delimiter
            yield buf.strip()[: -len(delim)].strip()
            buf = ""


if __name__ == "__main__":
    conn = mysql.connector.connect(host=os.getenv("DB_HOST", "localhost"), user=os.getenv("DB_USER", "root"),
                                   password=os.getenv("DB_PASS", ""))
    cur = conn.cursor()
    n = 0
    for st in statements(open(os.path.join(BASE, "schema.sql"), encoding="utf-8").read()):
        cur.execute(st)
        n += 1
    conn.commit()
    conn.close()
    print(f"Database ready: {n} statements executed. Now run: npm start")
