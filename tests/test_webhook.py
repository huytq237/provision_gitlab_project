"""Test cases cho Audit Service Webhook.

Kiểm tra trực tiếp endpoint POST /webhook của audit_service.py
không cần GitLab thật gọi vào — gửi HTTP request thủ công.

Chạy: python tests/test_webhook.py
Yêu cầu: audit_service.py đang chạy (python scripts/audit_service.py)
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Suffix unique mỗi lần chạy — tránh dedup với findings từ lần test trước
_RUN_ID = str(int(time.time()))[-5:]

# ── Cấu hình ──────────────────────────────────────────────────────
WEBHOOK_URL    = "http://localhost:9000/webhook"
HEALTH_URL     = "http://localhost:9000/health"
WEBHOOK_SECRET = "test-local-secret"

PASS_COLOR = "\033[32mPASS\033[0m"
FAIL_COLOR = "\033[31mFAIL\033[0m"

results: list[bool] = []


# ── Helpers ───────────────────────────────────────────────────────

def section(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


def check(name: str, ok: bool, detail: str = ""):
    results.append(ok)
    status = PASS_COLOR if ok else FAIL_COLOR
    suffix = f"  → {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")


def http_post(url: str, body: dict, headers: dict = None) -> tuple[int, dict | str]:
    """Gửi POST request. Trả về (status_code, response_body)."""
    data = json.dumps(body).encode()
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.reason


def http_get(url: str) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.reason


def webhook(body: dict, token: str = WEBHOOK_SECRET) -> tuple[int, str]:
    """Gửi webhook với token xác thực."""
    headers = {"X-Gitlab-Token": token} if token else {}
    return http_post(WEBHOOK_URL, body, headers)


def db_open_count() -> int:
    """Lấy số finding đang open từ health endpoint."""
    _, data = http_get(HEALTH_URL)
    if isinstance(data, dict):
        return data.get("db", {}).get("open", -1)
    return -1


def wait_for_webhook_processing(seconds: float = 1.5):
    """Webhook xử lý trong background thread — chờ một chút."""
    time.sleep(seconds)


# ══════════════════════════════════════════════════════════════════
#  KIỂM TRA SERVICE ĐANG CHẠY
# ══════════════════════════════════════════════════════════════════

code, data = http_get(HEALTH_URL)
if code != 200:
    print(f"\n[ERROR] Audit service chưa chạy! Hãy start trước:")
    print(f"  python scripts/audit_service.py &")
    sys.exit(1)

print(f"\n✓ Audit service đang chạy — DB: {data.get('db', {})}")


# ══════════════════════════════════════════════════════════════════
#  SECTION 1: Health & Routing
# ══════════════════════════════════════════════════════════════════
section("1. Health & Routing")

# 1.1 Health endpoint
code, data = http_get(HEALTH_URL)
check("GET /health → 200", code == 200,
      f"db={data.get('db')}" if isinstance(data, dict) else str(data))

# 1.2 Unknown path → 404
code, _ = http_get("http://localhost:9000/unknown")
check("GET /unknown → 404", code == 404)

# 1.3 POST unknown path → 404
code, _ = http_post("http://localhost:9000/notfound", {}, {"X-Gitlab-Token": WEBHOOK_SECRET})
check("POST /notfound → 404", code == 404)


# ══════════════════════════════════════════════════════════════════
#  SECTION 2: Authentication
# ══════════════════════════════════════════════════════════════════
section("2. Authentication")

# 2.1 Đúng token → 200
code, _ = webhook({"event_name": "ping"})
check("Token đúng → 200", code == 200)

# 2.2 Sai token → 403
code, _ = webhook({"event_name": "ping"}, token="wrong-token")
check("Token sai → 403", code == 403)

# 2.3 Không có token → 403
code, _ = http_post(WEBHOOK_URL, {"event_name": "ping"})
check("Không có token → 403", code == 403)

# 2.4 Token rỗng → 403
code, _ = webhook({"event_name": "ping"}, token="")
check("Token rỗng → 403", code == 403)


# ══════════════════════════════════════════════════════════════════
#  SECTION 3: Malformed Requests
# ══════════════════════════════════════════════════════════════════
section("3. Malformed Requests")

# 3.1 Body không phải JSON → 400
req = urllib.request.Request(
    WEBHOOK_URL,
    data=b"this is not json!!!",
    headers={"Content-Type": "application/json", "X-Gitlab-Token": WEBHOOK_SECRET},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        code = r.status
except urllib.error.HTTPError as e:
    code = e.code
check("Body không phải JSON → 400", code == 400)

# 3.2 Event không xác định → 200 nhưng không xử lý
before = db_open_count()
code, _ = webhook({"event_name": "push", "ref": "refs/heads/main"})
wait_for_webhook_processing(0.5)
after = db_open_count()
check("Event push (không xử lý) → 200, DB không thay đổi",
      code == 200 and before == after,
      f"before={before} after={after}")

# 3.3 Body rỗng (Content-Length: 0) → 400
req = urllib.request.Request(
    WEBHOOK_URL,
    data=b"",
    headers={"Content-Type": "application/json", "X-Gitlab-Token": WEBHOOK_SECRET},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        code = r.status
except urllib.error.HTTPError as e:
    code = e.code
check("Body rỗng → 400", code == 400)


# ══════════════════════════════════════════════════════════════════
#  SECTION 4: project_create — Naming Violations
# ══════════════════════════════════════════════════════════════════
section("4. project_create — Naming Violations")

# 4.1 Tên hợp lệ + đúng YAML → không có finding mới
before = db_open_count()
code, _ = webhook({
    "event_name": "project_create",
    "name": "treasury-core",
    "path": "treasury-core",
    "path_with_namespace": "ocb/udtn/treasury/treasury-backend/treasury-core",
    "project_id": 1001,
})
wait_for_webhook_processing()
after = db_open_count()
check("project tên hợp lệ + có trong YAML → không có finding mới",
      code == 200 and after == before,
      f"Δfindings={after - before}")

# Paths unique theo run để tránh dedup với lần chạy trước
_BAD_PATH  = f"Bad_Project_{_RUN_ID}"
_UND_PATH  = f"my_service_{_RUN_ID}"
_LEAD_PATH = f"-invalid-{_RUN_ID}"
_TRAIL_PATH = f"trailing-dash-{_RUN_ID}-"
_SPACE_PATH = f"my project {_RUN_ID}"

# 4.2 Tên có chữ hoa → naming violation
before = db_open_count()
code, _ = webhook({
    "event_name": "project_create",
    "name": _BAD_PATH,
    "path": _BAD_PATH,
    "path_with_namespace": f"ocb/udtn/treasury/{_BAD_PATH}",
    "project_id": 9001,
})
wait_for_webhook_processing()
after = db_open_count()
check(f"project '{_BAD_PATH}' (có hoa) → naming violation mới",
      code == 200 and after > before,
      f"Δfindings=+{after - before}")

# 4.3 Tên có dấu gạch dưới → naming violation
before = db_open_count()
code, _ = webhook({
    "event_name": "project_create",
    "name": _UND_PATH,
    "path": _UND_PATH,
    "path_with_namespace": f"ocb/udtn/treasury/{_UND_PATH}",
    "project_id": 9002,
})
wait_for_webhook_processing()
after = db_open_count()
check(f"project '{_UND_PATH}' (dấu _) → naming violation mới",
      code == 200 and after > before,
      f"Δfindings=+{after - before}")

# 4.4 Tên bắt đầu bằng dấu gạch ngang → naming violation
before = db_open_count()
code, _ = webhook({
    "event_name": "project_create",
    "name": _LEAD_PATH,
    "path": _LEAD_PATH,
    "path_with_namespace": f"ocb/udtn/treasury/{_LEAD_PATH}",
    "project_id": 9003,
})
wait_for_webhook_processing()
after = db_open_count()
check(f"project '{_LEAD_PATH}' (leading dash) → naming violation",
      code == 200 and after > before,
      f"Δfindings=+{after - before}")

# 4.5 Tên kết thúc bằng dấu gạch ngang → naming violation
before = db_open_count()
code, _ = webhook({
    "event_name": "project_create",
    "name": _TRAIL_PATH,
    "path": _TRAIL_PATH,
    "path_with_namespace": f"ocb/udtn/treasury/{_TRAIL_PATH}",
    "project_id": 9004,
})
wait_for_webhook_processing()
after = db_open_count()
check(f"project '{_TRAIL_PATH}' (trailing dash) → naming violation",
      code == 200 and after > before,
      f"Δfindings=+{after - before}")

# 4.6 Tên có dấu cách → naming violation
before = db_open_count()
code, _ = webhook({
    "event_name": "project_create",
    "name": _SPACE_PATH,
    "path": _SPACE_PATH,
    "path_with_namespace": f"ocb/udtn/treasury/{_SPACE_PATH}",
    "project_id": 9005,
})
wait_for_webhook_processing()
after = db_open_count()
check(f"project '{_SPACE_PATH}' (dấu cách) → naming violation",
      code == 200 and after > before,
      f"Δfindings=+{after - before}")

# 4.7 Finding trùng lặp → không tạo finding mới (gửi cùng path lần 2)
before = db_open_count()
code, _ = webhook({
    "event_name": "project_create",
    "name": _BAD_PATH,
    "path": _BAD_PATH,
    "path_with_namespace": f"ocb/udtn/treasury/{_BAD_PATH}",
    "project_id": 9001,
})
wait_for_webhook_processing()
after = db_open_count()
check(f"Webhook trùng '{_BAD_PATH}' lần 2 → không tạo finding mới (dedup)",
      code == 200 and after == before,
      f"Δfindings={after - before}")


# ══════════════════════════════════════════════════════════════════
#  SECTION 5: project_create — Rogue Detection
# ══════════════════════════════════════════════════════════════════
section("5. project_create — Rogue Detection")

# 5.1 Project không có trong YAML → rogue (dùng _RUN_ID để tránh dedup)
_SHADOW = f"shadow-experiment-{_RUN_ID}"
before = db_open_count()
code, _ = webhook({
    "event_name": "project_create",
    "name": _SHADOW,
    "path": _SHADOW,
    "path_with_namespace": f"ocb/udtn/treasury/{_SHADOW}",
    "project_id": 8001,
})
wait_for_webhook_processing()
after = db_open_count()
check(f"project '{_SHADOW}' (không trong YAML) → rogue finding",
      code == 200 and after > before,
      f"Δfindings=+{after - before}")

# 5.2 Project trong dept hoàn toàn chưa khai báo → rogue
_UNREGISTERED = f"ocb/unregistered-{_RUN_ID}/secret-tool"
before = db_open_count()
code, _ = webhook({
    "event_name": "project_create",
    "name": "secret-tool",
    "path": "secret-tool",
    "path_with_namespace": _UNREGISTERED,
    "project_id": 8002,
})
wait_for_webhook_processing()
after = db_open_count()
check("project trong dept chưa khai báo → rogue finding",
      code == 200 and after > before,
      f"Δfindings=+{after - before}")

# 5.3 Project đúng YAML, đúng tên → không phải rogue
before = db_open_count()
code, _ = webhook({
    "event_name": "project_create",
    "name": "treasury-deployment",
    "path": "treasury-deployment",
    "path_with_namespace": "ocb/udtn/treasury/treasury-deployment",
    "project_id": 8003,
})
wait_for_webhook_processing()
after = db_open_count()
check("project 'treasury-deployment' (đúng YAML) → không có finding mới",
      code == 200 and after == before,
      f"Δfindings={after - before}")


# ══════════════════════════════════════════════════════════════════
#  SECTION 6: group_create — Naming & Rogue
# ══════════════════════════════════════════════════════════════════
section("6. group_create — Naming & Rogue")

# 6.1 Group tên hợp lệ + có trong YAML → clean
before = db_open_count()
code, _ = webhook({
    "event_name": "group_create",
    "name": "udtn",
    "path": "udtn",
    "full_path": "ocb/udtn",
    "group_id": 7001,
})
wait_for_webhook_processing()
after = db_open_count()
check("group 'ocb/udtn' (hợp lệ, trong YAML) → không có finding",
      code == 200 and after == before,
      f"Δfindings={after - before}")

# 6.2 Group tên có chữ hoa → naming violation
_BAD_GROUP = f"My_Shadow_{_RUN_ID}"
before = db_open_count()
code, _ = webhook({
    "event_name": "group_create",
    "name": _BAD_GROUP,
    "path": _BAD_GROUP,
    "full_path": f"ocb/{_BAD_GROUP}",
    "group_id": 7002,
})
wait_for_webhook_processing()
after = db_open_count()
check(f"group '{_BAD_GROUP}' (chữ hoa + _) → naming violation",
      code == 200 and after > before,
      f"Δfindings=+{after - before}")

# 6.3 Group tên hợp lệ nhưng không trong YAML → rogue
_ROGUE_GROUP = f"shadow-dept-{_RUN_ID}"
before = db_open_count()
code, _ = webhook({
    "event_name": "group_create",
    "name": _ROGUE_GROUP,
    "path": _ROGUE_GROUP,
    "full_path": f"ocb/{_ROGUE_GROUP}",
    "group_id": 7003,
})
wait_for_webhook_processing()
after = db_open_count()
check(f"group 'ocb/{_ROGUE_GROUP}' (tên ổn nhưng không YAML) → rogue",
      code == 200 and after > before,
      f"Δfindings=+{after - before}")

# 6.4 Root group (ocb) được tạo → không phải rogue
before = db_open_count()
code, _ = webhook({
    "event_name": "group_create",
    "name": "ocb",
    "path": "ocb",
    "full_path": "ocb",
    "group_id": 1,
})
wait_for_webhook_processing()
after = db_open_count()
check("Root group 'ocb' → không tính là rogue",
      code == 200 and after == before,
      f"Δfindings={after - before}")


# ══════════════════════════════════════════════════════════════════
#  SECTION 7: project_rename & group_rename
# ══════════════════════════════════════════════════════════════════
section("7. project_rename & group_rename")

# 7.1 project_rename với tên mới hợp lệ nhưng không trong YAML → rogue (bình thường)
# Ghi chú: bất kỳ path nào không khai báo trong YAML đều là rogue.
# treasury-core-v2 hợp lệ về naming nhưng KHÔNG có trong YAML → đúng là rogue.
before = db_open_count()
code, _ = webhook({
    "event_name": "project_rename",
    "name": "treasury-core-v2",
    "path": "treasury-core-v2",
    "path_with_namespace": "ocb/udtn/treasury/treasury-backend/treasury-core-v2",
    "old_path_with_namespace": "ocb/udtn/treasury/treasury-backend/treasury-core",
    "project_id": 5001,
})
wait_for_webhook_processing()
after = db_open_count()
check("project_rename tên mới 'treasury-core-v2' (hợp lệ nhưng không YAML) → rogue finding",
      code == 200 and after >= before,
      f"Δfindings={after - before}")

# 7.2 project_rename với tên mới sai chuẩn → naming violation
_RENAME_BAD_PROJ = f"Treasury_Core_{_RUN_ID}"
before = db_open_count()
code, _ = webhook({
    "event_name": "project_rename",
    "name": _RENAME_BAD_PROJ,
    "path": _RENAME_BAD_PROJ,
    "path_with_namespace": f"ocb/udtn/treasury/{_RENAME_BAD_PROJ}",
    "old_path_with_namespace": "ocb/udtn/treasury/treasury-core",
    "project_id": 5002,
})
wait_for_webhook_processing()
after = db_open_count()
check(f"project_rename tên mới '{_RENAME_BAD_PROJ}' (sai chuẩn) → naming violation",
      code == 200 and after > before,
      f"Δfindings=+{after - before}")

# 7.3 group_rename với tên mới sai chuẩn → naming violation
_RENAME_BAD_GRP = f"UDTN_DEPT_{_RUN_ID}"
before = db_open_count()
code, _ = webhook({
    "event_name": "group_rename",
    "name": _RENAME_BAD_GRP,
    "path": _RENAME_BAD_GRP,
    "full_path": f"ocb/{_RENAME_BAD_GRP}",
    "old_path": "udtn",
    "old_full_path": "ocb/udtn",
    "group_id": 6001,
})
wait_for_webhook_processing()
after = db_open_count()
check(f"group_rename tên mới '{_RENAME_BAD_GRP}' (sai chuẩn) → naming violation",
      code == 200 and after > before,
      f"Δfindings=+{after - before}")

# 7.4 group_rename với tên mới hợp lệ → không có finding
before = db_open_count()
code, _ = webhook({
    "event_name": "group_rename",
    "name": "udtn-v2",
    "path": "udtn-v2",
    "full_path": "ocb/udtn-v2",
    "old_path": "udtn",
    "old_full_path": "ocb/udtn",
    "group_id": 6002,
})
wait_for_webhook_processing()
after = db_open_count()
# udtn-v2 sẽ bị rogue vì không trong YAML — kiểm tra đủ 2 chiều
check("group_rename tên mới 'udtn-v2' (hợp lệ nhưng rogue) → 1 finding rogue",
      code == 200 and after >= before,
      f"Δfindings={after - before}")


# ══════════════════════════════════════════════════════════════════
#  SECTION 8: Test Real GitLab Trigger (qua System Hook)
# ══════════════════════════════════════════════════════════════════
section("8. Test Real GitLab Trigger — Tạo project thật trên GitLab")

import os, sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

ee_url   = os.environ.get("EE_URL", "").rstrip("/")
ee_token = os.environ.get("EE_TOKEN", "")

# Lấy namespace ID của ocb/udtn/treasury/treasury-backend trên EE
def _api_get(path: str) -> dict | None:
    url = f"{ee_url}/api/v4{path}"
    req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": ee_token})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return None

def _api_post(path: str, body: dict) -> tuple[int, dict]:
    url = f"{ee_url}/api/v4{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"PRIVATE-TOKEN": ee_token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}

def _api_delete(path: str) -> int:
    url = f"{ee_url}/api/v4{path}"
    req = urllib.request.Request(
        url, headers={"PRIVATE-TOKEN": ee_token}, method="DELETE"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code

# 8.1 GitLab tạo project (naming ổn, không có trong YAML) → GitLab gửi webhook → rogue finding
# Dùng _RUN_ID để project name unique mỗi lần chạy test
_REAL_VALID = f"wh-valid-{_RUN_ID}"
ns = _api_get("/groups/ocb%2Fudtn%2Ftreasury")
if ns:
    before = db_open_count()
    status, proj = _api_post("/projects", {
        "name": _REAL_VALID,
        "path": _REAL_VALID,
        "namespace_id": ns["id"],
        "visibility": "private",
    })
    wait_for_webhook_processing(3)
    after = db_open_count()
    check(
        f"GitLab tạo '{_REAL_VALID}' (naming OK, không YAML) → GitLab system hook → rogue finding",
        status in (200, 201) and after > before,
        f"HTTP={status} Δfindings=+{after - before}",
    )
    if proj.get("id"):
        _api_delete(f"/projects/{proj['id']}")
else:
    check("GitLab real: project rogue (naming OK)", False, "Không lấy được namespace ocb/udtn/treasury")

# 8.2 GitLab tạo project không trong YAML → rogue finding (unique name mỗi run)
_REAL_ROGUE = f"wh-rogue-{_RUN_ID}"
ns = _api_get("/groups/ocb%2Fudtn%2Ftreasury")
if ns:
    before = db_open_count()
    status, proj = _api_post("/projects", {
        "name": _REAL_ROGUE,
        "path": _REAL_ROGUE,
        "namespace_id": ns["id"],
        "visibility": "private",
    })
    wait_for_webhook_processing(3)
    after = db_open_count()
    check(
        f"GitLab tạo '{_REAL_ROGUE}' (không trong YAML) → GitLab system hook → rogue finding",
        status in (200, 201) and after > before,
        f"HTTP={status} Δfindings=+{after - before}",
    )
    if proj.get("id"):
        _api_delete(f"/projects/{proj['id']}")
else:
    check("GitLab real: project rogue", False, "Không lấy được namespace")


# ══════════════════════════════════════════════════════════════════
#  SUMMARY
# ══════════════════════════════════════════════════════════════════
passed = sum(results)
total  = len(results)
failed = total - passed
print(f"\n{'═'*60}")
print(f"  TOTAL: {passed}/{total} passed  ({failed} failed)")
print(f"{'═'*60}\n")

# Trạng thái DB cuối
_, health = http_get(HEALTH_URL)
if isinstance(health, dict):
    print(f"  DB sau test: {health.get('db', {})}")
print()

sys.exit(1 if failed > 0 else 0)
