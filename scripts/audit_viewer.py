"""Xem nội dung audit.db trực tiếp từ terminal.

Dùng:
    python scripts/audit_viewer.py                  # tất cả findings đang open
    python scripts/audit_viewer.py --all            # kể cả đã resolved
    python scripts/audit_viewer.py --violation naming
    python scripts/audit_viewer.py --violation rogue
    python scripts/audit_viewer.py --instance EE
    python scripts/audit_viewer.py --summary
"""

import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

DB_PATH = os.environ.get("AUDIT_DB_PATH", str(ROOT / "data" / "audit.db"))

# ANSI colors
RED    = "\033[31m"
YELLOW = "\033[33m"
GREEN  = "\033[32m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def _connect() -> sqlite3.Connection:
    if not Path(DB_PATH).exists():
        print(f"[ERROR] Không tìm thấy DB: {DB_PATH}")
        raise SystemExit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _violation_color(v: str) -> str:
    return RED if v == "naming" else YELLOW


def _age(ts: str) -> str:
    """Tính khoảng cách từ detected_at đến bây giờ."""
    try:
        dt = datetime.fromisoformat(ts)
        diff = datetime.now(timezone.utc) - dt
        s = int(diff.total_seconds())
        if s < 60:
            return f"{s}s ago"
        if s < 3600:
            return f"{s//60}m ago"
        if s < 86400:
            return f"{s//3600}h ago"
        return f"{s//86400}d ago"
    except Exception:
        return ts[:19]


def cmd_summary(conn: sqlite3.Connection):
    """Hiển thị bảng tóm tắt."""
    rows = conn.execute("""
        SELECT
            violation,
            instance,
            COUNT(*) FILTER (WHERE resolved_at IS NULL)     AS open,
            COUNT(*) FILTER (WHERE resolved_at IS NOT NULL) AS resolved,
            COUNT(*) FILTER (WHERE alerted_at  IS NOT NULL
                             AND   resolved_at IS NULL)     AS alerted
        FROM findings
        GROUP BY violation, instance
        ORDER BY violation, instance
    """).fetchall()

    total = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    open_ = conn.execute("SELECT COUNT(*) FROM findings WHERE resolved_at IS NULL").fetchone()[0]

    print(f"\n{BOLD}{'═'*52}{RESET}")
    print(f"{BOLD}  AUDIT DB SUMMARY   {DB_PATH}{RESET}")
    print(f"{BOLD}{'═'*52}{RESET}")
    print(f"  {'VIOLATION':<10} {'INST':<5} {'OPEN':>6} {'RESOLVED':>9} {'ALERTED':>8}")
    print(f"  {'─'*10} {'─'*5} {'─'*6} {'─'*9} {'─'*8}")
    for r in rows:
        vc = _violation_color(r["violation"])
        print(f"  {vc}{r['violation']:<10}{RESET} {r['instance']:<5} "
              f"{r['open']:>6} {r['resolved']:>9} {r['alerted']:>8}")
    print(f"  {'─'*42}")
    print(f"  {'TOTAL':<16} {open_:>6} open  /  {total} total")
    print(f"{BOLD}{'═'*52}{RESET}\n")


def cmd_list(conn: sqlite3.Connection, args):
    """Hiển thị danh sách findings dạng bảng."""
    where_clauses = []
    params = []

    if not args.all:
        where_clauses.append("resolved_at IS NULL")
    if args.violation:
        where_clauses.append("violation = ?")
        params.append(args.violation)
    if args.instance:
        where_clauses.append("instance = ?")
        params.append(args.instance.upper())

    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    sql = f"""
        SELECT id, instance, kind, violation, full_path, detail,
               detected_at, alerted_at, resolved_at
        FROM findings
        {where}
        ORDER BY detected_at DESC
        LIMIT 200
    """
    rows = conn.execute(sql, params).fetchall()

    if not rows:
        print("\n  (Không có findings nào.)\n")
        return

    # Header
    print(f"\n{BOLD}{'─'*100}{RESET}")
    print(f"{BOLD}  {'ID':>4}  {'INST':<4}  {'KIND':<8}  {'TYPE':<7}  {'AGE':<10}  {'PATH / DETAIL'}{RESET}")
    print(f"{'─'*100}")

    for r in rows:
        vc = _violation_color(r["violation"])
        status = ""
        if r["resolved_at"]:
            status = f" {GREEN}[RESOLVED]{RESET}"
        elif r["alerted_at"]:
            status = f" {CYAN}[ALERTED]{RESET}"

        age = _age(r["detected_at"])
        print(f"  {r['id']:>4}  {r['instance']:<4}  {r['kind']:<8}  "
              f"{vc}{r['violation']:<7}{RESET}  {age:<10}  {r['full_path']}{status}")

        if r["detail"]:
            print(f"  {'':>4}  {'':4}  {'':8}  {'':7}  {'':10}  "
                  f"{CYAN}→ suggested: {r['detail']}{RESET}")

    print(f"{'─'*100}")
    print(f"  Hiển thị {len(rows)} findings"
          + (" (open)" if not args.all else " (all)")
          + ("\n"))


def main():
    parser = argparse.ArgumentParser(description="Xem nội dung audit.db")
    parser.add_argument("--all", action="store_true",
                        help="Hiển thị kể cả findings đã resolved")
    parser.add_argument("--violation", choices=["naming", "rogue"],
                        help="Lọc theo loại vi phạm")
    parser.add_argument("--instance", choices=["EE", "CE", "ee", "ce"],
                        help="Lọc theo GitLab instance")
    parser.add_argument("--summary", action="store_true",
                        help="Chỉ hiển thị bảng tóm tắt")
    args = parser.parse_args()

    conn = _connect()

    if args.summary:
        cmd_summary(conn)
    else:
        cmd_summary(conn)
        cmd_list(conn, args)

    conn.close()


if __name__ == "__main__":
    main()
