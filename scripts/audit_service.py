"""GitLab Audit Service — daemon chạy liên tục.

Hai chế độ hoạt động song song:
  1. Webhook server  : nhận GitLab system hook khi có group/project mới tạo → validate ngay
  2. Polling scanner : định kỳ scan toàn bộ GitLab → phát hiện legacy + rogue repos

Chạy trực tiếp:
    python scripts/audit_service.py

Deploy systemd:
    sudo cp systemd/gitlab-audit.service /etc/systemd/system/
    sudo systemctl enable --now gitlab-audit
    journalctl -u gitlab-audit -f

Cấu hình GitLab System Hook (Admin Area → System Hooks):
    URL   : http://<server-ip>:9000/webhook
    Token : giá trị WEBHOOK_SECRET trong .env
    Không cần tích thêm checkbox — project_create và group_create
    là administrative events, GitLab gửi tự động.
"""

import http.server
import json
import logging
import os
import queue
import signal
import smtplib
import sys
import threading
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

# ── Config ────────────────────────────────────────────────────────
ROOT_GROUP            = os.environ.get("ROOT_GROUP", "ocb")
WEBHOOK_PORT          = int(os.environ.get("WEBHOOK_PORT", "9000"))
WEBHOOK_SECRET        = os.environ.get("WEBHOOK_SECRET", "")
SCAN_INTERVAL_MINUTES = int(os.environ.get("SCAN_INTERVAL_MINUTES", "60"))
AUDIT_DB_PATH         = os.environ.get("AUDIT_DB_PATH", str(ROOT / "data" / "audit.db"))
ALERT_EMAIL           = os.environ.get("ALERT_EMAIL", "")
SMTP_HOST             = os.environ.get("SMTP_HOST", "localhost")
SMTP_PORT             = int(os.environ.get("SMTP_PORT", "25"))
SMTP_FROM             = os.environ.get("SMTP_FROM", "gitlab-audit@noreply.local")
SMTP_USER             = os.environ.get("SMTP_USER", "")
SMTP_PASS             = os.environ.get("SMTP_PASS", "")
SMTP_TLS              = os.environ.get("SMTP_TLS", "false").lower() == "true"

logging.basicConfig(
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── Local imports ─────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from gitlab_client import GitLabClient  # noqa: E402
from audit_checks import (              # noqa: E402
    NamingIssue, RogueItem,
    check_naming_single, build_expected_set, traverse_gitlab,
)
from audit_db import AuditDB            # noqa: E402

PROJECTS_DIR = ROOT / "projects"

# ── Shared state (khởi tạo trong main()) ─────────────────────────
_db: AuditDB = None
_alert_queue: queue.Queue = None
_stop_event: threading.Event = None
_clients: list[GitLabClient] = []


# ══════════════════════════════ Email ═════════════════════════════

def _format_alert_body(findings: list) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "=" * 60,
        "GITLAB AUDIT ALERT",
        f"Time      : {ts}",
        f"ROOT_GROUP: {ROOT_GROUP}",
        "=" * 60,
        "",
        f"{len(findings)} finding(s) mới phát hiện:",
        "",
    ]
    for f in findings:
        if isinstance(f, NamingIssue):
            lines.append(
                f"  [NAMING] [{f.instance}] {f.kind.upper()} '{f.full_path}'\n"
                f"           Hiện tại: '{f.current_path}'  →  Đề xuất: '{f.suggested_path}'"
            )
        elif isinstance(f, RogueItem):
            lines.append(
                f"  [ROGUE]  [{f.instance}] {f.kind.upper()} '{f.full_path}'\n"
                f"           Không có trong YAML provisioning"
            )
        lines.append("")
    lines += [
        "─" * 60,
        "Hành động cần thực hiện:",
        "  Naming  → review và rename trực tiếp trên GitLab",
        "  Rogue   → thêm vào projects/ YAML hoặc xóa khỏi GitLab",
        "=" * 60,
    ]
    return "\n".join(lines)


def _send_email(to_addr: str, subject: str, body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr
    msg.attach(MIMEText(body, "plain", "utf-8"))
    try:
        if SMTP_TLS:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
                if SMTP_USER:
                    s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(SMTP_FROM, [to_addr], msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
                s.ehlo()
                if SMTP_USER:
                    s.starttls()
                    s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(SMTP_FROM, [to_addr], msg.as_string())
        log.info("Alert email đã gửi → %s", to_addr)
    except Exception as e:
        log.error("Gửi email thất bại: %s", e)


# ══════════════════════════════ Alert worker ══════════════════════

def alert_worker():
    """Gom các findings trong 60 giây rồi gửi 1 email — tránh spam."""
    buffer: list = []
    last_flush = time.time()
    FLUSH_INTERVAL = 60

    while not _stop_event.is_set():
        try:
            items = _alert_queue.get(timeout=5)
            buffer.extend(items)
        except queue.Empty:
            pass

        now = time.time()
        if buffer and (now - last_flush >= FLUSH_INTERVAL):
            if ALERT_EMAIL:
                subject = f"[GitLab Audit] {len(buffer)} finding(s) mới"
                _send_email(ALERT_EMAIL, subject, _format_alert_body(buffer))

            for item in buffer:
                violation = "naming" if isinstance(item, NamingIssue) else "rogue"
                _db.mark_alerted(item.instance, item.full_path, violation)

            buffer.clear()
            last_flush = now


# ══════════════════════════════ Webhook handler ═══════════════════

def _process_webhook_event(event: dict):
    """Xử lý một GitLab system hook event trong background thread."""
    event_name = event.get("event_name", "")

    if event_name not in ("project_create", "group_create",
                           "project_rename", "group_rename"):
        return

    # Xác định instance từ URL nếu có (system hook không cung cấp trực tiếp)
    # Mặc định gán EE — webhook riêng có thể config cho từng instance
    instance = event.get("_instance", "EE")

    if event_name in ("project_create", "project_rename"):
        path_seg  = event.get("path", "")
        full_path = event.get("path_with_namespace", "")
        item_id   = int(event.get("project_id", 0))
        kind      = "project"
    else:
        path_seg  = event.get("path", "")
        full_path = event.get("full_path", "")
        item_id   = int(event.get("group_id", 0))
        kind      = "group"

    if not full_path or not path_seg:
        return

    new_findings: list = []

    # Check naming
    issue = check_naming_single(path_seg, full_path, kind, item_id, instance)
    if issue:
        is_new = _db.upsert_finding(instance, kind, full_path, "naming", issue.suggested_path)
        if is_new:
            log.warning("[WEBHOOK][NAMING] %s %s '%s' → đề xuất: '%s'",
                        instance, kind, full_path, issue.suggested_path)
            new_findings.append(issue)

    # Check rogue
    try:
        expected_groups, expected_projects = build_expected_set(PROJECTS_DIR)
        expected = expected_projects if kind == "project" else expected_groups
        if full_path not in expected and full_path != ROOT_GROUP:
            rogue = RogueItem(kind, item_id, full_path, instance)
            is_new = _db.upsert_finding(instance, kind, full_path, "rogue", "")
            if is_new:
                log.warning("[WEBHOOK][ROGUE] %s %s '%s'", instance, kind, full_path)
                new_findings.append(rogue)
    except Exception as e:
        log.error("[WEBHOOK] Không load được expected set: %s", e)

    if new_findings:
        _alert_queue.put(new_findings)


class _WebhookHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            summary = _db.summary() if _db else {}
            body = json.dumps({"status": "ok", "db": summary}).encode()
            self._respond(200, "application/json", body)
        else:
            self._respond(404, "text/plain", b"Not found")

    def do_POST(self):
        if self.path != "/webhook":
            self._respond(404, "text/plain", b"Not found")
            return

        if WEBHOOK_SECRET:
            token = self.headers.get("X-Gitlab-Token", "")
            if token != WEBHOOK_SECRET:
                self._respond(403, "text/plain", b"Forbidden")
                return

        length = int(self.headers.get("Content-Length", 0))
        try:
            event = json.loads(self.rfile.read(length))
        except Exception:
            self._respond(400, "text/plain", b"Bad JSON")
            return

        # Xử lý bất đồng bộ để không block response
        threading.Thread(
            target=_process_webhook_event, args=(event,), daemon=True
        ).start()

        self._respond(200, "text/plain", b"OK")

    def _respond(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.debug("WEBHOOK " + fmt, *args)


# ══════════════════════════════ Polling scanner ═══════════════════

def _run_scan():
    """Một lần scan toàn bộ GitLab. Gọi từ polling_worker."""
    log.info("─── Bắt đầu full scan ───")
    new_findings: list = []

    try:
        expected_groups, expected_projects = build_expected_set(PROJECTS_DIR)
        log.info("Expected: %d groups, %d projects từ YAML",
                 len(expected_groups), len(expected_projects))
    except Exception as e:
        log.error("Không load được expected set: %s", e)
        expected_groups, expected_projects = set(), set()

    for client in _clients:
        try:
            all_groups, all_projects = traverse_gitlab(client, ROOT_GROUP)
            log.info("[%s] Tìm thấy: %d groups, %d projects",
                     client.label, len(all_groups), len(all_projects))
        except Exception as e:
            log.error("[%s] Scan thất bại: %s", client.label, e)
            continue

        actual_paths = (
            {g["full_path"] for g in all_groups}
            | {p["path_with_namespace"] for p in all_projects}
        )

        # Naming check — groups
        for g in all_groups:
            issue = check_naming_single(
                g["path"], g["full_path"], "group", g["id"], client.label
            )
            if issue:
                if _db.upsert_finding(client.label, "group",
                                      g["full_path"], "naming", issue.suggested_path):
                    log.warning("[SCAN][NAMING] %s group '%s'", client.label, g["full_path"])
                    new_findings.append(issue)

        # Naming check — projects
        for p in all_projects:
            issue = check_naming_single(
                p["path"], p["path_with_namespace"], "project", p["id"], client.label
            )
            if issue:
                if _db.upsert_finding(client.label, "project",
                                      p["path_with_namespace"], "naming", issue.suggested_path):
                    log.warning("[SCAN][NAMING] %s project '%s'", client.label, p["path_with_namespace"])
                    new_findings.append(issue)

        # Rogue check — groups
        for g in all_groups:
            if g["full_path"] == ROOT_GROUP:
                continue
            if g["full_path"] not in expected_groups:
                rogue = RogueItem("group", g["id"], g["full_path"], client.label)
                if _db.upsert_finding(client.label, "group", g["full_path"], "rogue", ""):
                    log.warning("[SCAN][ROGUE] %s group '%s'", client.label, g["full_path"])
                    new_findings.append(rogue)

        # Rogue check — projects
        for p in all_projects:
            if p["path_with_namespace"] not in expected_projects:
                rogue = RogueItem("project", p["id"], p["path_with_namespace"], client.label)
                if _db.upsert_finding(client.label, "project",
                                      p["path_with_namespace"], "rogue", ""):
                    log.warning("[SCAN][ROGUE] %s project '%s'", client.label, p["path_with_namespace"])
                    new_findings.append(rogue)

        _db.sync_resolved(client.label, actual_paths)

    summary = _db.summary()
    log.info("─── Scan xong: %d finding mới | DB open=%d resolved=%d",
             len(new_findings), summary["open"], summary["resolved"])

    if new_findings:
        _alert_queue.put(new_findings)


def polling_worker():
    # Chờ 10 giây sau khi khởi động trước khi scan lần đầu
    if _stop_event.wait(timeout=10):
        return

    while not _stop_event.is_set():
        try:
            _run_scan()
        except Exception as e:
            log.exception("Scan loop exception: %s", e)

        _stop_event.wait(timeout=SCAN_INTERVAL_MINUTES * 60)


# ══════════════════════════════ Main ══════════════════════════════

def main():
    global _db, _alert_queue, _stop_event, _clients

    ee_url   = os.environ.get("EE_URL", "").rstrip("/")
    ee_token = os.environ.get("EE_TOKEN", "")
    ce_url   = os.environ.get("CE_URL", "").rstrip("/")
    ce_token = os.environ.get("CE_TOKEN", "")

    if not all([ee_url, ee_token, ce_url, ce_token]):
        log.error("Thiếu biến môi trường: EE_URL, EE_TOKEN, CE_URL, CE_TOKEN")
        sys.exit(1)

    _db          = AuditDB(AUDIT_DB_PATH)
    _alert_queue = queue.Queue()
    _stop_event  = threading.Event()

    _clients = [
        GitLabClient(ee_url, ee_token, label="EE"),
        GitLabClient(ce_url, ce_token, label="CE"),
    ]

    for client in _clients:
        if not client.ping():
            log.error("Không kết nối được %s", client.label)
            sys.exit(1)

    server = http.server.ThreadingHTTPServer(("0.0.0.0", WEBHOOK_PORT), _WebhookHandler)

    threads = [
        threading.Thread(target=server.serve_forever,  name="webhook", daemon=True),
        threading.Thread(target=polling_worker,         name="poller",  daemon=True),
        threading.Thread(target=alert_worker,           name="alerter", daemon=True),
    ]
    for t in threads:
        t.start()

    log.info("GitLab Audit Service đã khởi động")
    log.info("  Webhook : http://0.0.0.0:%d/webhook", WEBHOOK_PORT)
    log.info("  Health  : http://0.0.0.0:%d/health",  WEBHOOK_PORT)
    log.info("  Scan    : mỗi %d phút", SCAN_INTERVAL_MINUTES)
    log.info("  DB      : %s", AUDIT_DB_PATH)
    if ALERT_EMAIL:
        log.info("  Email   : %s", ALERT_EMAIL)
    if not WEBHOOK_SECRET:
        log.warning("  WEBHOOK_SECRET chưa đặt — endpoint /webhook không có auth!")

    def _shutdown(sig, frame):
        log.info("Nhận tín hiệu shutdown (%s) — đang dừng...", sig)
        _stop_event.set()
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    _stop_event.wait()
    log.info("Service đã dừng.")


def run_once(mode: str, alert_email: str = ""):
    """One-shot audit: chạy một lần, in report, thoát.

    Dùng trong CI pipeline hoặc chạy tay:
        python scripts/audit_service.py --once --mode all
        python scripts/audit_service.py --once --mode naming --alert-email ops@example.com
    """
    global _db, _alert_queue, _stop_event, _clients

    ee_url   = os.environ.get("EE_URL", "").rstrip("/")
    ee_token = os.environ.get("EE_TOKEN", "")
    ce_url   = os.environ.get("CE_URL", "").rstrip("/")
    ce_token = os.environ.get("CE_TOKEN", "")

    if not all([ee_url, ee_token, ce_url, ce_token]):
        log.error("Thiếu biến môi trường: EE_URL, EE_TOKEN, CE_URL, CE_TOKEN")
        sys.exit(1)

    _db          = AuditDB(AUDIT_DB_PATH)
    _alert_queue = queue.Queue()
    _stop_event  = threading.Event()

    _clients = [
        GitLabClient(ee_url, ee_token, label="EE"),
        GitLabClient(ce_url, ce_token, label="CE"),
    ]
    for client in _clients:
        if not client.ping():
            log.error("Không kết nối được %s", client.label)
            sys.exit(1)

    # Build expected set
    try:
        expected_groups, expected_projects = build_expected_set(PROJECTS_DIR)
    except Exception as e:
        log.error("Không load được expected set: %s", e)
        expected_groups, expected_projects = set(), set()

    all_findings: list = []

    for client in _clients:
        try:
            all_groups, all_projects = traverse_gitlab(client, ROOT_GROUP)
        except Exception as e:
            log.error("[%s] Traverse thất bại: %s", client.label, e)
            continue

        if mode in ("naming", "all"):
            for g in all_groups:
                issue = check_naming_single(g["path"], g["full_path"], "group", g["id"], client.label)
                if issue:
                    _db.upsert_finding(client.label, "group", g["full_path"], "naming", issue.suggested_path)
                    all_findings.append(issue)
            for p in all_projects:
                issue = check_naming_single(p["path"], p["path_with_namespace"], "project", p["id"], client.label)
                if issue:
                    _db.upsert_finding(client.label, "project", p["path_with_namespace"], "naming", issue.suggested_path)
                    all_findings.append(issue)

        if mode in ("rogue", "all"):
            for g in all_groups:
                if g["full_path"] == ROOT_GROUP:
                    continue
                if g["full_path"] not in expected_groups:
                    rogue = RogueItem("group", g["id"], g["full_path"], client.label)
                    _db.upsert_finding(client.label, "group", g["full_path"], "rogue", "")
                    all_findings.append(rogue)
            for p in all_projects:
                if p["path_with_namespace"] not in expected_projects:
                    rogue = RogueItem("project", p["id"], p["path_with_namespace"], client.label)
                    _db.upsert_finding(client.label, "project", p["path_with_namespace"], "rogue", "")
                    all_findings.append(rogue)

    # Print report
    naming_issues = [f for f in all_findings if isinstance(f, NamingIssue)]
    rogue_items   = [f for f in all_findings if isinstance(f, RogueItem)]

    print("=" * 60)
    print("GITLAB AUDIT REPORT (one-shot)")
    print(f"Mode: {mode} | ROOT_GROUP: {ROOT_GROUP}")
    print("=" * 60)

    if mode in ("naming", "all"):
        print(f"\n── NAMING CONVENTION")
        if naming_issues:
            print(f"   [FAIL] {len(naming_issues)} vi phạm:")
            for i in naming_issues:
                print(f"   [FAIL] [{i.instance}] {i.kind.upper()} '{i.full_path}'"
                      f" → đề xuất: '{i.suggested_path}'")
        else:
            print("   [PASS] Tất cả đều đúng chuẩn.")

    if mode in ("rogue", "all"):
        print(f"\n── ROGUE REPOS")
        if rogue_items:
            print(f"   [WARN] {len(rogue_items)} rogue item(s):")
            for r in rogue_items:
                print(f"   [WARN] [{r.instance}] {r.kind.upper()} '{r.full_path}'")
        else:
            print("   [PASS] Không phát hiện rogue repo.")

    summary = _db.summary()
    print(f"\n── SUMMARY")
    print(f"   Naming issues : {len(naming_issues)}")
    print(f"   Rogue items   : {len(rogue_items)}")
    print(f"   DB open       : {summary['open']}")
    print("=" * 60)

    # Gửi email nếu có findings và có địa chỉ nhận
    if all_findings and (alert_email or ALERT_EMAIL):
        dest = alert_email or ALERT_EMAIL
        subject = f"[GitLab Audit] {len(naming_issues)} naming, {len(rogue_items)} rogue"
        _send_email(dest, subject, _format_alert_body(all_findings))
        for item in all_findings:
            violation = "naming" if isinstance(item, NamingIssue) else "rogue"
            _db.mark_alerted(item.instance, item.full_path, violation)

    sys.exit(1 if naming_issues else 0)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="GitLab Audit Service")
    parser.add_argument("--once", action="store_true",
                        help="Chạy một lần (one-shot) rồi thoát, không start daemon")
    parser.add_argument("--mode", choices=["naming", "rogue", "all"], default="all",
                        help="Loại audit khi dùng --once (default: all)")
    parser.add_argument("--alert-email", metavar="ADDR",
                        help="Ghi đè ALERT_EMAIL env var cho lần chạy này")
    args = parser.parse_args()

    if args.once:
        run_once(mode=args.mode, alert_email=args.alert_email or "")
        return

    # ── Daemon mode ──────────────────────────────────────────────
    global _db, _alert_queue, _stop_event, _clients

    ee_url   = os.environ.get("EE_URL", "").rstrip("/")
    ee_token = os.environ.get("EE_TOKEN", "")
    ce_url   = os.environ.get("CE_URL", "").rstrip("/")
    ce_token = os.environ.get("CE_TOKEN", "")

    if not all([ee_url, ee_token, ce_url, ce_token]):
        log.error("Thiếu biến môi trường: EE_URL, EE_TOKEN, CE_URL, CE_TOKEN")
        sys.exit(1)

    _db          = AuditDB(AUDIT_DB_PATH)
    _alert_queue = queue.Queue()
    _stop_event  = threading.Event()

    _clients = [
        GitLabClient(ee_url, ee_token, label="EE"),
        GitLabClient(ce_url, ce_token, label="CE"),
    ]

    for client in _clients:
        if not client.ping():
            log.error("Không kết nối được %s", client.label)
            sys.exit(1)

    server = http.server.ThreadingHTTPServer(("0.0.0.0", WEBHOOK_PORT), _WebhookHandler)

    threads = [
        threading.Thread(target=server.serve_forever,  name="webhook", daemon=True),
        threading.Thread(target=polling_worker,         name="poller",  daemon=True),
        threading.Thread(target=alert_worker,           name="alerter", daemon=True),
    ]
    for t in threads:
        t.start()

    log.info("GitLab Audit Service đã khởi động")
    log.info("  Webhook : http://0.0.0.0:%d/webhook", WEBHOOK_PORT)
    log.info("  Health  : http://0.0.0.0:%d/health",  WEBHOOK_PORT)
    log.info("  Scan    : mỗi %d phút", SCAN_INTERVAL_MINUTES)
    log.info("  DB      : %s", AUDIT_DB_PATH)
    if ALERT_EMAIL:
        log.info("  Email   : %s", ALERT_EMAIL)
    if not WEBHOOK_SECRET:
        log.warning("  WEBHOOK_SECRET chưa đặt — endpoint /webhook không có auth!")

    def _shutdown(sig, frame):
        log.info("Nhận tín hiệu shutdown (%s) — đang dừng...", sig)
        _stop_event.set()
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    _stop_event.wait()
    log.info("Service đã dừng.")


if __name__ == "__main__":
    main()
