# Tài Liệu Kỹ Thuật — GitLab Provisioner & Audit Service

**Phiên bản:** 1.1  
**Ngày cập nhật:** 2026-05-20  
**Tác giả:** OCB DevSecOps

---

## Mục Lục

1. [Tổng Quan](#1-tổng-quan)
2. [Kiến Trúc Hệ Thống](#2-kiến-trúc-hệ-thống)
3. [Cấu Trúc Thư Mục](#3-cấu-trúc-thư-mục)
4. [Quy Tắc Khai Báo YAML](#4-quy-tắc-khai-báo-yaml)
5. [Luồng GitOps — Từ YAML Đến GitLab](#5-luồng-gitops--từ-yaml-đến-gitlab)
6. [Các Script Chính](#6-các-script-chính)
7. [Audit Service](#7-audit-service)
8. [Đọc Audit Database](#8-đọc-audit-database)
9. [CI/CD Pipeline](#9-cicd-pipeline)
10. [Môi Trường Local (Docker)](#10-môi-trường-local-docker)
11. [Cấu Hình Biến Môi Trường](#11-cấu-hình-biến-môi-trường)
12. [Deploy Systemd (Production)](#12-deploy-systemd-production)
13. [Test Suite](#13-test-suite)
14. [Xử Lý Sự Cố](#14-xử-lý-sự-cố)

---

## 1. Tổng Quan

**GitLab Provisioner** là công cụ GitOps tự động hóa việc tạo cấu trúc group/subgroup/project trên **hai GitLab instance** của OCB:

| Instance | URL (Production) | Vai Trò |
|---|---|---|
| GitLab EE | `gitlab-dso.ocb.vn` | DevSecOps platform (CI/CD, security scanning) |
| GitLab CE | `git.ocb.vn` | Source code chính của các team |

**Nguyên tắc hoạt động:**
- Developer khai báo cấu trúc project trong file YAML
- Mở Merge Request → CI tự động validate YAML
- Sau khi merge vào `main` → CI tự động tạo groups/projects trên cả 2 GitLab
- Mọi thao tác đều **idempotent** — chạy lại không tạo duplicate

**Audit Service** chạy song song như một daemon độc lập:
- Nhận webhook real-time khi có group/project mới được tạo trực tiếp trên GitLab
- Định kỳ scan toàn bộ GitLab để phát hiện repo cũ sai chuẩn hoặc "tạo chui"
- Lưu lịch sử vào SQLite, gửi email alert khi phát hiện vi phạm mới

---

## 2. Kiến Trúc Hệ Thống

```
┌─────────────────────────────────────────────────────────────────┐
│                    Git Repository (GitLab)                       │
│  projects/                                                       │
│    UDTN/treasury.yml          ← Developer khai báo YAML         │
│    KTTHUD/test-project.yml                                       │
└───────────────────────────┬─────────────────────────────────────┘
                            │ MR / Push
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                      CI/CD Pipeline                             │
│                                                                 │
│  Stage: validate          Stage: provision    Stage: audit      │
│  ┌──────────────┐         ┌─────────────┐    ┌─────────────┐   │
│  │validate:yaml │         │provision:   │    │audit:naming │   │
│  │validate:all  │ ──────► │changed      │    │audit:rogue  │   │
│  └──────────────┘         │provision:   │    │audit:all    │   │
│   scripts/validate.py     │all (manual) │    └─────────────┘   │
│                           └──────┬──────┘                       │
└──────────────────────────────────┼──────────────────────────────┘
                                   │ GitLab REST API v4
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
         ┌──────────────────┐         ┌──────────────────┐
         │   GitLab EE      │         │   GitLab CE      │
         │ gitlab-dso.ocb.vn│         │   git.ocb.vn     │
         │                  │         │                  │
         │ ocb/             │         │ ocb/             │
         │  └─ udtn/        │         │  └─ udtn/        │
         │      └─ treasury/│         │      └─ treasury/│
         │          └─ ...  │         │          └─ ...  │
         └──────────────────┘         └──────────────────┘
                    ▲                             ▲
                    │  System Hook                │
                    └──────────────┬──────────────┘
                                   │
         ┌─────────────────────────▼────────────────────┐
         │              Audit Service (Daemon)           │
         │                                              │
         │  ┌─────────────┐  ┌─────────────────────┐   │
         │  │Webhook :9000│  │ Polling (60 phút)   │   │
         │  │POST /webhook│  │ traverse_gitlab()   │   │
         │  └──────┬──────┘  └──────────┬──────────┘   │
         │         └──────────┬──────────┘              │
         │                    ▼                         │
         │  ┌─────────────────────────────────────┐    │
         │  │  audit_checks.py                    │    │
         │  │  check_naming_single()              │    │
         │  │  build_expected_set() → compare     │    │
         │  └─────────────┬───────────────────────┘    │
         │                ▼                             │
         │  ┌─────────────────────┐  ┌──────────────┐  │
         │  │   SQLite DB         │  │ Email Alert  │  │
         │  │   data/audit.db     │  │ (SMTP)       │  │
         │  └──────────┬──────────┘  └──────────────┘  │
         │             │ audit_viewer.py                 │
         └─────────────┼────────────────────────────────┘
                       ▼
              Terminal / Ops team
```

---

## 3. Cấu Trúc Thư Mục

```
provision_gitlab_project/
│
├── projects/                    ← Khai báo GitLab projects (YAML)
│   ├── UDTN/
│   │   └── treasury.yml
│   └── KTTHUD/
│       └── test-project.yml
│
├── scripts/                     ← Toàn bộ logic Python
│   ├── gitlab_client.py         ← GitLab REST API client
│   ├── validate.py              ← Validate YAML trước khi provision
│   ├── provision.py             ← Tạo groups/projects trên GitLab
│   ├── audit_checks.py          ← Logic kiểm tra naming + rogue (stateless)
│   ├── audit_db.py              ← SQLite layer lưu audit findings
│   ├── audit_service.py         ← Daemon: webhook server + polling scanner
│   └── audit_viewer.py          ← CLI xem nội dung audit.db trên terminal
│
├── schema/
│   └── project_schema.json      ← JSON Schema validate cấu trúc YAML
│
├── templates/
│   └── project_template.yml     ← Template mẫu cho developer
│
├── tests/
│   ├── run_tests.py             ← Test runner (35 test cases)
│   ├── test_webhook.py          ← Webhook test suite (30 test cases)
│   ├── valid/                   ← 5 YAML hợp lệ
│   ├── invalid/                 ← 9 YAML sai để test rejection
│   └── duplicate/               ← 2 YAML trùng dept/app
│
├── systemd/
│   └── gitlab-audit.service     ← Systemd unit cho audit daemon
│
├── data/
│   └── audit.db                 ← SQLite DB (tự tạo khi chạy)
│
├── docker-compose.yml           ← 2 GitLab CE cho test local
├── .env                         ← Biến môi trường (không commit)
├── .env.example                 ← Template biến môi trường
├── .gitlab-ci.yml               ← CI/CD pipeline định nghĩa
├── requirements.txt             ← Python dependencies
└── TECHNICAL.md                 ← Tài liệu này
```

---

## 4. Quy Tắc Khai Báo YAML

### 4.1 Cấu Trúc File

Mỗi application được khai báo trong một file YAML riêng, đặt trong `projects/<DEPARTMENT>/`.

```yaml
# projects/UDTN/treasury.yml

# [BẮT BUỘC] Tên department → tạo Group cấp 1 trên GitLab
department: udtn

# [BẮT BUỘC] Tên application → tạo Subgroup cấp 2
application: treasury

# [TÙY CHỌN] Các component cần tạo
components:

  documents:
    enabled: true           # → project: ocb/udtn/treasury/treasury-documents

  backend:
    enabled: true
    services:               # → mỗi service là 1 project trong ocb/udtn/treasury/treasury-backend/
      - treasury-core
      - treasury-reporting
      - treasury-integration
    common_lib: true        # → project: treasury-common-lib

  frontend:
    enabled: true
    apps:                   # → mỗi app là 1 project trong ocb/udtn/treasury/treasury-frontend/
      - treasury-web
    ui_libs: false

  tools:
    enabled: false          # → project: treasury-tools

  deployment:
    enabled: true           # → project: treasury-deployment

  quality_security:
    enabled: true           # → project: treasury-quality-security
```

### 4.2 Quy Tắc Đặt Tên (Slug Convention)

Pattern: `^[a-z0-9][a-z0-9-]*[a-z0-9]$`

| Quy tắc | Ví dụ hợp lệ | Ví dụ sai |
|---|---|---|
| Chỉ chữ thường, số, dấu `-` | `udtn`, `core-banking` | `UDTN`, `Core_Banking` |
| Không bắt đầu hoặc kết thúc bằng `-` | `payment-gw` | `-payment`, `gw-` |
| Tối thiểu 2 ký tự | `ab`, `udtn` | `a` (1 ký tự) |
| Tối đa 64 ký tự | `treasury-core-service` | (chuỗi > 64 ký tự) |

### 4.3 Cấu Trúc Group Được Tạo

Với khai báo `department: udtn`, `application: treasury`, hệ thống tạo:

```
ocb/                              ← ROOT_GROUP (mặc định: "ocb")
  udtn/                           ← Department group
    treasury/                     ← Application subgroup
      treasury-documents          ← Project (nếu documents.enabled)
      treasury-backend/           ← Backend subgroup (nếu backend.enabled)
        treasury-core             ← Service projects
        treasury-reporting
        treasury-integration
        treasury-common-lib       ← Nếu common_lib: true
      treasury-frontend/          ← Frontend subgroup (nếu frontend.enabled)
        treasury-web              ← App projects
        treasury-ui-libs          ← Nếu ui_libs: true
      treasury-tools              ← Project (nếu tools.enabled)
      treasury-deployment         ← Project (nếu deployment.enabled)
      treasury-quality-security   ← Project (nếu quality_security.enabled)
```

### 4.4 Các Lỗi Validate Phổ Biến

| Lỗi | Nguyên nhân | Cách sửa |
|---|---|---|
| Schema error | Thiếu field bắt buộc hoặc có field lạ | Xem `schema/project_schema.json` |
| Naming violation | Tên chứa chữ hoa, dấu cách, `_` | Đổi sang kebab-case chữ thường |
| Duplicate | Hai file cùng `department/application` | Mỗi application chỉ khai báo 1 lần |
| Single char | `department: a` (1 ký tự) | GitLab yêu cầu tối thiểu 2 ký tự |

---

## 5. Luồng GitOps — Từ YAML Đến GitLab

```
Developer                Git Repo                 CI Pipeline              GitLab
    │                       │                          │                      │
    │  1. Tạo/sửa YAML      │                          │                      │
    │──────────────────────►│                          │                      │
    │                       │                          │                      │
    │  2. Mở Merge Request  │                          │                      │
    │──────────────────────►│                          │                      │
    │                       │  3. Trigger validate     │                      │
    │                       │─────────────────────────►│                      │
    │                       │                          │  validate.py         │
    │                       │                          │  ┌───────────────┐   │
    │                       │                          │  │ YAML syntax   │   │
    │                       │                          │  │ JSON Schema   │   │
    │                       │                          │  │ Naming rules  │   │
    │                       │                          │  │ Dup check     │   │
    │                       │                          │  └───────────────┘   │
    │  4. Pass/Fail feedback │                          │                      │
    │◄──────────────────────│◄─────────────────────────│                      │
    │                       │                          │                      │
    │  5. Review + Approve  │                          │                      │
    │──────────────────────►│                          │                      │
    │                       │                          │                      │
    │  6. Merge vào main    │                          │                      │
    │──────────────────────►│  7. Trigger provision    │                      │
    │                       │─────────────────────────►│                      │
    │                       │                          │  provision.py         │
    │                       │                          │  ┌───────────────┐   │
    │                       │                          │  │ build_plan()  │   │
    │                       │                          │  │ create groups │──►│
    │                       │                          │  │ create projs  │──►│
    │                       │                          │  │ (EE + CE)     │   │
    │                       │                          │  └───────────────┘   │
    │  8. Done              │                          │                      │
    │◄──────────────────────│◄─────────────────────────│                      │
```

---

## 6. Các Script Chính

### 6.1 `scripts/gitlab_client.py` — GitLab REST API Client

Wrapper cho GitLab REST API v4, dùng chung cho tất cả scripts.

**Class `GitLabClient`:**

| Method | Mô tả |
|---|---|
| `__init__(url, token, label)` | Tạo session với `PRIVATE-TOKEN` header |
| `ping()` | Kiểm tra kết nối và token hợp lệ |
| `get_group(full_path)` | Lấy group theo full path, trả về `None` nếu không tồn tại |
| `create_group(name, path, parent_id, visibility)` | Tạo group idempotent |
| `ensure_group_path(full_path)` | Tạo toàn bộ nested path, từng cấp một |
| `get_project(full_path)` | Lấy project theo full path |
| `create_project(name, path, namespace_id, ...)` | Tạo project idempotent |
| `_get(path, params)` | GET request, trả về `None` nếu 404 |
| `_post(path, data)` | POST request |
| `_put(path, data)` | PUT request (dùng cho rename) |
| `_get_list(path, params)` | GET với auto-pagination qua header `X-Next-Page` |
| `list_subgroups(group_id)` | Liệt kê direct subgroups |
| `list_group_projects(group_id)` | Liệt kê projects trong group |
| `list_descendant_groups(group_id)` | Liệt kê tất cả groups con (mọi cấp) |
| `rename_group(group_id, new_path, new_name)` | Đổi tên group |
| `rename_project(project_id, new_path, new_name)` | Đổi tên project |

**Đặc điểm:**
- **Idempotent:** Kiểm tra tồn tại trước khi tạo
- **Pagination:** `_get_list()` tự động đọc header `X-Next-Page`, dừng khi header trống
- **Timeout:** 30 giây cho tất cả API calls

### 6.2 `scripts/validate.py` — Validate YAML

Kiểm tra file YAML trước khi provision. Dùng trong CI stage `validate`.

**Cách dùng:**
```bash
# Validate file cụ thể
python scripts/validate.py --files projects/UDTN/treasury.yml

# Validate toàn bộ thư mục projects/
python scripts/validate.py --all
```

**Bốn lớp kiểm tra (theo thứ tự):**

```
1. YAML Syntax     → yaml.safe_load() — parse lỗi thì dừng luôn
2. JSON Schema     → jsonschema.validate() theo schema/project_schema.json
3. Naming Conv.    → SLUG_RE = ^[a-z0-9][a-z0-9-]*[a-z0-9]$
                     Kiểm tra: department, application, services[], apps[]
4. Duplicate Check → Theo dõi dict seen[dept/app] → báo lỗi nếu trùng
```

**Exit codes:** `0` = tất cả PASS, `1` = có ít nhất 1 lỗi

### 6.3 `scripts/provision.py` — Tạo Groups/Projects

**Cách dùng:**
```bash
# Provision file trong CI (chỉ file thay đổi so với commit trước)
python scripts/provision.py --changed-only

# Provision file cụ thể
python scripts/provision.py --files projects/UDTN/treasury.yml

# Preview không gọi API
python scripts/provision.py --files projects/UDTN/treasury.yml --dry-run

# Provision toàn bộ (manual resync)
python scripts/provision.py --all
```

**Logic `build_plan(data)`:**

Từ YAML → `ProvisionPlan` với 2 danh sách:
- `plan.groups: list[str]` — full paths của groups cần tạo, theo thứ tự từ cấp 1 → sâu nhất
- `plan.projects: list[tuple[str, str]]` — `(parent_group_path, project_path)`

```python
# Ví dụ với treasury.yml:
plan.groups = [
    "ocb",
    "ocb/udtn",
    "ocb/udtn/treasury",
    "ocb/udtn/treasury/treasury-backend",
    "ocb/udtn/treasury/treasury-frontend",
]
plan.projects = [
    ("ocb/udtn/treasury", "treasury-documents"),
    ("ocb/udtn/treasury/treasury-backend", "treasury-core"),
    ...
]
```

**`provision_plan(plan, client, dry_run)`:**
1. Tạo từng group theo thứ tự (idempotent qua `ensure_group_path`)
2. Tạo từng project (kiểm tra tồn tại trước)
3. Trả về `(created, skipped, errors)`
4. Chạy song song trên cả EE và CE

**`ROOT_GROUP`:** Biến môi trường (mặc định: `ocb`). Tất cả groups đều là subgroup của root này.

---

## 7. Audit Service

### 7.1 Tổng Quan

`scripts/audit_service.py` chạy như một **daemon** độc lập (hoặc one-shot trong CI). Gồm 3 thành phần:

| Component | Thread | Vai trò |
|---|---|---|
| Webhook Server | Thread 1 | HTTP server nhận GitLab system hook |
| Polling Scanner | Thread 2 | Định kỳ scan toàn bộ GitLab |
| Alert Worker | Thread 3 | Gom findings và gửi email (60 giây/lần) |

### 7.2 Chế Độ Hoạt Động

**Daemon mode** (dùng với systemd):
```bash
python scripts/audit_service.py
```

**One-shot mode** (dùng trong CI hoặc kiểm tra thủ công):
```bash
python scripts/audit_service.py --once --mode naming
python scripts/audit_service.py --once --mode rogue
python scripts/audit_service.py --once --mode all
python scripts/audit_service.py --once --mode all --alert-email ops@example.com
```

### 7.3 Webhook Server (`/webhook`)

**Cấu hình GitLab System Hook qua API:**
```bash
# Tạo system hook trên GitLab EE
curl -X POST https://gitlab-dso.ocb.vn/api/v4/hooks \
  -H "PRIVATE-TOKEN: <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://<server-ip>:9000/webhook",
    "token": "<WEBHOOK_SECRET>",
    "push_events": false,
    "tag_push_events": false,
    "merge_requests_events": false,
    "repository_update_events": false
  }'

# Tương tự trên GitLab CE
curl -X POST https://git.ocb.vn/api/v4/hooks ...
```

Hoặc vào thủ công: **Admin Area → System Hooks → Add new webhook**

| Field | Giá trị |
|---|---|
| URL | `http://<server-ip>:9000/webhook` |
| Secret Token | Giá trị `WEBHOOK_SECRET` trong `.env` |
| Checkboxes | Không cần tích thêm gì |

> **Giải thích:** `project_create`, `project_rename`, `group_create`, `group_rename` là administrative events — GitLab gửi tự động mà không cần tích checkbox.

**Events được xử lý:**

| Event | Trigger | Kiểm tra |
|---|---|---|
| `project_create` | Khi ai đó tạo project mới trên GitLab | Naming + Rogue |
| `group_create` | Khi ai đó tạo group mới | Naming + Rogue |
| `project_rename` | Khi đổi tên project | Naming + Rogue |
| `group_rename` | Khi đổi tên group | Naming + Rogue |

**Endpoints:**

| Endpoint | Method | Mô tả |
|---|---|---|
| `/webhook` | POST | Nhận GitLab system hook. Yêu cầu header `X-Gitlab-Token` |
| `/health` | GET | Health check, trả về DB summary dạng JSON |

**Ví dụ health response:**
```json
{
  "status": "ok",
  "db": {
    "open": 5,
    "resolved": 12,
    "alerted": 3
  }
}
```

**HTTP response codes webhook:**

| Code | Tình huống |
|---|---|
| `200` | Event nhận và xử lý thành công |
| `400` | Body không phải JSON hoặc rỗng |
| `403` | `X-Gitlab-Token` sai hoặc thiếu |
| `404` | Path không phải `/webhook` |

### 7.4 Polling Scanner

Chạy sau 10 giây khởi động, sau đó lặp lại mỗi `SCAN_INTERVAL_MINUTES` phút.

**Quy trình mỗi lần scan:**
```
1. build_expected_set(projects/)       → đọc tất cả YAML → expected groups + projects
2. traverse_gitlab(client, ROOT_GROUP) → lấy actual state từ GitLab API
3. Naming check  → so sánh path segment với SLUG_RE
4. Rogue check   → actual - expected = unauthorized repos
5. sync_resolved → mark resolved những finding đã biến mất
6. Queue alert   → nếu có finding mới
```

### 7.5 Kiểm Tra Naming Convention

**Function `check_naming_single(path_segment, full_path, kind, id, instance)`:**
- Lấy `path` (segment cuối, không có slash) của group/project
- So khớp với `SLUG_RE = ^[a-z0-9][a-z0-9-]*[a-z0-9]$`
- Nếu không khớp → tạo `NamingIssue` với `suggested_path = slugify(path_segment)`

**Function `slugify(s)`:**
```
"Treasury_App"  →  "treasury-app"
"My Service"    →  "my-service"
"UPPER.CASE"    →  "upper-case"
"a--b"          →  "a-b"
"-leading"      →  "leading"
"trailing-"     →  "trailing"
"---"           →  ""  (không thể tạo slug → skip rename)
```

### 7.6 Phát Hiện Rogue Repos

**Function `check_rogue(groups, projects, expected_groups, expected_projects, ...)`:**
- Build expected set từ tất cả `*.yml` trong `projects/`
- Bất kỳ `full_path` nào trên GitLab mà **không có trong expected set** → rogue
- Root group (`ocb`) luôn được bỏ qua (không cần khai báo trong YAML)

**Ví dụ rogue:** Developer tạo project `ocb/udtn/treasury/shadow-experiment` trực tiếp trên GitLab mà không có YAML tương ứng → audit service phát hiện và alert.

### 7.7 SQLite Database (`data/audit.db`)

File SQLite lưu trên disk máy chủ chạy audit service. Đường dẫn cấu hình qua `AUDIT_DB_PATH` (mặc định: `data/audit.db`). Thư mục và file được tạo tự động khi service khởi động.

**Bảng `findings`:**

| Cột | Kiểu | Mô tả |
|---|---|---|
| `id` | INTEGER PK | Auto increment |
| `instance` | TEXT | `"EE"` hoặc `"CE"` |
| `kind` | TEXT | `"group"` hoặc `"project"` |
| `full_path` | TEXT | VD: `ocb/udtn/treasury/Bad_Project` |
| `violation` | TEXT | `"naming"` hoặc `"rogue"` |
| `detail` | TEXT | Với naming: suggested slug |
| `detected_at` | TEXT | ISO8601 UTC |
| `alerted_at` | TEXT | NULL nếu chưa gửi email |
| `resolved_at` | TEXT | NULL nếu vẫn còn vi phạm |

**Ràng buộc UNIQUE:** `(instance, full_path, violation)` — tránh duplicate.

**Logic upsert:**
- Finding mới → INSERT → trả về `True` (cần alert)
- Finding đã tồn tại, chưa resolved → trả về `False` (không alert lại)
- Finding đã resolved trước đây, nay xuất hiện lại → reopen → trả về `True`

**Thread safety:** `threading.Lock` bảo vệ tất cả write operations vì webhook handler và polling scanner chạy trên 2 thread khác nhau.

### 7.8 Email Alert

- Gom tất cả findings trong 60 giây (tránh spam nhiều email)
- Chỉ gửi khi có findings **mới** (không spam cho finding đã biết)
- Hỗ trợ SMTP thường (`STARTTLS`) và SMTP SSL (`SMTP_TLS=true`)
- Cấu hình qua biến môi trường: `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_USER`, `SMTP_PASS`, `SMTP_TLS`

**Format email:**
```
Subject: [GitLab Audit] 3 finding(s) mới

============================================================
GITLAB AUDIT ALERT
Time      : 2026-05-20 08:00:00 UTC
ROOT_GROUP: ocb
============================================================

3 finding(s) mới phát hiện:

  [NAMING] [EE] PROJECT 'ocb/udtn/treasury/Bad_Project'
           Hiện tại: 'Bad_Project'  →  Đề xuất: 'bad-project'

  [ROGUE]  [CE] GROUP 'ocb/shadow-group'
           Không có trong YAML provisioning
...
```

### 7.9 `scripts/audit_checks.py` — Pure Functions

File stateless, không có I/O ngoài logging. Dùng chung cho cả webhook lẫn polling.

| Function | Mô tả |
|---|---|
| `slugify(s)` | Convert string thành valid slug |
| `check_naming_single(path_seg, full_path, kind, id, instance)` | Kiểm tra 1 group/project |
| `build_expected_set(projects_dir)` | Đọc tất cả YAML → `(set[groups], set[projects])` |
| `traverse_gitlab(client, root_group)` | Lấy toàn bộ groups + projects từ GitLab API |

---

## 8. Đọc Audit Database

`data/audit.db` là file SQLite nhị phân. Có 3 cách đọc:

### 8.1 `scripts/audit_viewer.py` (Khuyến nghị)

Script CLI hiển thị findings dạng bảng có màu, không cần cài thêm gì.

```bash
# Xem tóm tắt + danh sách findings đang open
python scripts/audit_viewer.py

# Chỉ xem bảng tóm tắt (open/resolved/alerted theo instance và loại)
python scripts/audit_viewer.py --summary

# Lọc theo loại vi phạm
python scripts/audit_viewer.py --violation naming
python scripts/audit_viewer.py --violation rogue

# Lọc theo GitLab instance
python scripts/audit_viewer.py --instance EE
python scripts/audit_viewer.py --instance CE

# Xem tất cả kể cả đã resolved
python scripts/audit_viewer.py --all

# Kết hợp nhiều bộ lọc
python scripts/audit_viewer.py --violation naming --instance EE --all
```

**Ví dụ output:**
```
════════════════════════════════════════════════════
  AUDIT DB SUMMARY   data/audit.db
════════════════════════════════════════════════════
  VIOLATION  INST    OPEN  RESOLVED  ALERTED
  ────────── ───── ────── ───────── ────────
  naming     EE        28         0       28
  rogue      CE        26         0       26
  rogue      EE        66         0       66
  ──────────────────────────────────────────
  TOTAL               120 open  /  120 total
════════════════════════════════════════════════════

────────────────────────────────────────────────────
    ID  INST  KIND      TYPE     AGE        PATH / DETAIL
────────────────────────────────────────────────────
   119  EE    project   naming   4m ago     ocb/udtn/treasury/Bad_Project [ALERTED]
                                            → suggested: bad-project
   118  EE    project   rogue    4m ago     ocb/udtn/treasury/shadow-exp [ALERTED]
```

### 8.2 `sqlite3` CLI

```bash
# Cài (nếu chưa có)
sudo apt install sqlite3

# Mở DB
sqlite3 data/audit.db

# Trong sqlite3 shell:
.mode column
.headers on

-- Xem tất cả findings đang open
SELECT instance, kind, violation, full_path, detected_at
FROM findings
WHERE resolved_at IS NULL
ORDER BY detected_at DESC;

-- Chỉ naming violations
SELECT * FROM findings WHERE violation='naming' AND resolved_at IS NULL;

-- Tóm tắt theo nhóm
SELECT instance, violation, COUNT(*) AS cnt
FROM findings WHERE resolved_at IS NULL
GROUP BY instance, violation;

.quit
```

### 8.3 GUI — DB Browser for SQLite

```bash
# Cài trên Ubuntu
sudo apt install sqlitebrowser

# Mở file
sqlitebrowser data/audit.db
```

Hoặc dùng extension **SQLite Viewer** trên VS Code — kéo thả file `data/audit.db` vào IDE.

---

## 9. CI/CD Pipeline

### 9.1 Tổng Quan Stages

```yaml
stages:
  - validate    # Kiểm tra YAML trước khi merge
  - provision   # Tạo groups/projects sau khi merge
  - audit       # Audit định kỳ (manual hoặc schedule)
```

### 9.2 Chi Tiết Các Jobs

| Job | Stage | Trigger | Mô tả |
|---|---|---|---|
| `validate:yaml` | validate | Mỗi MR | Validate chỉ file YAML thay đổi trong MR |
| `validate:all` | validate | Push main (nếu có thay đổi YAML/schema) | Validate toàn bộ `projects/` |
| `provision:changed` | provision | Push main (nếu có thay đổi YAML) | Provision chỉ file thay đổi |
| `provision:all` | provision | Manual | Provision lại toàn bộ (sync) |
| `audit:naming` | audit | Schedule (`AUDIT_MODE=naming`) hoặc manual | Kiểm tra naming convention |
| `audit:rogue` | audit | Schedule (`AUDIT_MODE=rogue`) hoặc manual | Phát hiện rogue repos |
| `audit:all` | audit | Schedule (`AUDIT_MODE=all`) hoặc manual | Kiểm tra toàn diện |

### 9.3 CI/CD Variables Cần Cấu Hình

Vào **Settings → CI/CD → Variables** (masked):

| Biến | Mô tả |
|---|---|
| `EE_URL` | URL GitLab EE (VD: `https://gitlab-dso.ocb.vn`) |
| `EE_TOKEN` | Personal/Group Access Token của EE (scope: `api`) |
| `CE_URL` | URL GitLab CE (VD: `https://git.ocb.vn`) |
| `CE_TOKEN` | Personal/Group Access Token của CE (scope: api) |
| `ROOT_GROUP` | Group gốc (mặc định: `ocb`) |
| `ALERT_EMAIL` | Email nhận audit alert (optional) |
| `AUDIT_MODE` | `naming`/`rogue`/`all` — dùng cho CI Schedule |

### 9.4 Cấu Hình Schedule

Vào **CI/CD → Schedules**, tạo schedule:
- **Cron:** `0 6 * * *` (6:00 AM hàng ngày)
- **Branch:** `main`
- **Variables:** `AUDIT_MODE=all`, `ALERT_EMAIL=ops@example.com`

---

## 10. Môi Trường Local (Docker)

### 10.1 Khởi Động

```bash
# Start 2 GitLab CE containers
docker compose up -d

# Theo dõi tiến trình khởi động (mất ~3-5 phút lần đầu)
docker logs -f gitlab-ee

# Lấy root password (lần đầu)
docker exec gitlab-ee grep 'Password:' /etc/gitlab/initial_root_password
docker exec gitlab-ce grep 'Password:' /etc/gitlab/initial_root_password
```

| Instance | URL | SSH Port |
|---|---|---|
| GitLab EE (simulated) | http://localhost:8080 | 2222 |
| GitLab CE (simulated) | http://localhost:8081 | 2223 |

### 10.2 Tạo Access Token

```bash
# Tạo token tự động qua Rails runner (không cần UI)
docker exec gitlab-ee gitlab-rails runner "
  user = User.find_by_username('root')
  token = user.personal_access_tokens.create!(
    name: 'provision-token',
    scopes: ['api'],
    expires_at: 1.year.from_now
  )
  puts token.token
"
```

Điền token vào `.env`: `EE_TOKEN=glpat-...`

### 10.3 Cấu Hình System Hook Local

Trong môi trường Docker, GitLab container cần gọi về audit service trên host. IP host từ container là `172.18.0.1` (gateway của Docker compose network).

```bash
# Kiểm tra IP host từ container
docker inspect gitlab-ee --format '{{range .NetworkSettings.Networks}}Gateway: {{.Gateway}}{{end}}'

# Tạo system hook trên EE local
curl -X POST http://localhost:8080/api/v4/hooks \
  -H "PRIVATE-TOKEN: <root-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://172.18.0.1:9000/webhook",
    "token": "test-local-secret",
    "push_events": false,
    "tag_push_events": false,
    "merge_requests_events": false,
    "repository_update_events": false
  }'
```

### 10.4 Lưu Ý Cấu Hình Docker

- `external_url 'http://localhost'` (KHÔNG phải `http://localhost:8080`) — GitLab Puma bind port 80, Docker map 8080:80
- Monitoring services bị tắt (`prometheus`, `alertmanager`, v.v.) để tiết kiệm RAM

### 10.5 Chạy Test Local

```bash
# Cài dependencies
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# Chạy test suite (35 test cases — cần Docker GitLab cho section 5-6)
.venv/bin/python tests/run_tests.py

# Chạy webhook test suite (30 test cases — cần audit service đang chạy)
python scripts/audit_service.py &          # start daemon
.venv/bin/python tests/test_webhook.py
```

---

## 11. Cấu Hình Biến Môi Trường

Copy `.env.example` thành `.env`:

```bash
cp .env.example .env
```

### Tất Cả Biến

| Biến | Mặc định | Bắt buộc | Mô tả |
|---|---|---|---|
| `ROOT_GROUP` | `ocb` | Không | Group gốc chứa toàn bộ cấu trúc |
| `EE_URL` | — | **Có** | URL GitLab EE |
| `EE_TOKEN` | — | **Có** | Access token GitLab EE (scope: api) |
| `CE_URL` | — | **Có** | URL GitLab CE |
| `CE_TOKEN` | — | **Có** | Access token GitLab CE (scope: api) |
| `WEBHOOK_PORT` | `9000` | Không | Port webhook server lắng nghe |
| `WEBHOOK_SECRET` | `""` | Khuyến nghị | Secret token xác thực webhook từ GitLab |
| `SCAN_INTERVAL_MINUTES` | `60` | Không | Khoảng cách giữa các lần polling scan |
| `AUDIT_DB_PATH` | `data/audit.db` | Không | Đường dẫn file SQLite |
| `ALERT_EMAIL` | `""` | Không | Email nhận alert (trống = không gửi) |
| `SMTP_HOST` | `localhost` | Không | SMTP server hostname |
| `SMTP_PORT` | `25` | Không | SMTP port |
| `SMTP_FROM` | `gitlab-audit@noreply.local` | Không | Địa chỉ người gửi |
| `SMTP_USER` | `""` | Không | SMTP username (để trống nếu không cần auth) |
| `SMTP_PASS` | `""` | Không | SMTP password |
| `SMTP_TLS` | `false` | Không | `true` = dùng SMTP_SSL, `false` = STARTTLS |

---

## 12. Deploy Systemd (Production)

### 12.1 Cài Đặt

```bash
# Copy unit file vào systemd
sudo cp systemd/gitlab-audit.service /etc/systemd/system/

# Reload và enable
sudo systemctl daemon-reload
sudo systemctl enable gitlab-audit

# Khởi động
sudo systemctl start gitlab-audit

# Kiểm tra status
sudo systemctl status gitlab-audit
```

### 12.2 Quản Lý Service

```bash
# Xem log real-time
journalctl -u gitlab-audit -f

# Xem log 100 dòng cuối
journalctl -u gitlab-audit -n 100

# Restart khi thay đổi code
sudo systemctl restart gitlab-audit

# Dừng service
sudo systemctl stop gitlab-audit
```

### 12.3 Nội Dung Unit File

```ini
[Unit]
Description=GitLab Audit Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ht
WorkingDirectory=/home/ht/provision_gitlab_project
ExecStart=/home/ht/provision_gitlab_project/.venv/bin/python scripts/audit_service.py
Restart=on-failure
RestartSec=30
EnvironmentFile=/home/ht/provision_gitlab_project/.env
StandardOutput=journal
StandardError=journal
MemoryMax=256M
CPUQuota=20%

[Install]
WantedBy=multi-user.target
```

---

## 13. Test Suite

### 13.1 Provisioning Tests — `tests/run_tests.py` (35 test cases)

```bash
.venv/bin/python tests/run_tests.py
```

| Section | TC | Cần GitLab | Mô tả |
|---|---|---|---|
| 1. Validate valid | 5 | Không | 5 YAML hợp lệ phải PASS |
| 2. Validate invalid | 9 | Không | 9 YAML sai phải bị REJECT |
| 3. Duplicate | 1 | Không | 2 file cùng dept/app phải FAIL |
| 4. Dry-run | 5 | Không | Dry-run không crash, exit 0 |
| 5. Provision lần 1 | 5 | **Có** | Tạo resources, `errors=0` |
| 6. Provision lần 2 | 5 | **Có** | Idempotent: `created=0, errors=0` |
| 7. Missing file | 1 | Không | Graceful skip, exit 0 |
| 8. Audit modules | 4 | Không | Import, slugify, AuditDB, syntax |

### 13.2 Webhook Tests — `tests/test_webhook.py` (30 test cases)

```bash
# Yêu cầu: audit_service.py đang chạy
python scripts/audit_service.py &
.venv/bin/python tests/test_webhook.py
```

| Section | TC | Mô tả |
|---|---|---|
| 1. Health & Routing | 3 | `GET /health` → 200, unknown path → 404 |
| 2. Authentication | 4 | Token đúng → 200, sai/rỗng/thiếu → 403 |
| 3. Malformed Requests | 3 | JSON lỗi → 400, body rỗng → 400, event lạ → 200 không xử lý |
| 4. project_create Naming | 7 | Chữ hoa / underscore / leading dash / trailing dash / dấu cách + dedup |
| 5. project_create Rogue | 3 | Không trong YAML / dept chưa đăng ký / đúng YAML (clean) |
| 6. group_create | 4 | Naming OK + YAML / naming xấu / rogue / root group exempt |
| 7. project_rename & group_rename | 4 | Tên mới hợp lệ / tên mới sai chuẩn cho cả project lẫn group |
| 8. Real GitLab trigger | 2 | GitLab tạo project thật → system hook → service detect rogue |

**Lưu ý quan trọng về Section 8:** Mọi project tạo trực tiếp trên GitLab mà không có khai báo trong `projects/` đều bị phát hiện là **rogue** — kể cả khi tên đặt đúng chuẩn. Đây là hành vi đúng của hệ thống.

### 13.3 Test Fixtures

**Valid** (`tests/valid/`):

| File | Đặc điểm |
|---|---|
| `minimal.yml` | Chỉ department + application, không component |
| `documents-only.yml` | Chỉ component documents |
| `backend-empty-services.yml` | Backend enabled nhưng services rỗng |
| `numeric-name.yml` | Tên chứa số (dept123/app456) |
| `full-stack.yml` | Đầy đủ tất cả components |

**Invalid** (`tests/invalid/`):

| File | Lỗi |
|---|---|
| `broken-yaml.yml` | Cú pháp YAML sai |
| `missing-department.yml` | Thiếu field bắt buộc |
| `missing-application.yml` | Thiếu field bắt buộc |
| `uppercase-dept.yml` | Chữ hoa trong tên |
| `leading-dash.yml` | Bắt đầu bằng `-` |
| `trailing-dash.yml` | Kết thúc bằng `-` |
| `single-char.yml` | Tên 1 ký tự |
| `service-with-space.yml` | Service name có dấu cách |
| `unknown-field.yml` | Field lạ không trong schema |

---

## 14. Xử Lý Sự Cố

### 14.1 Provision Thất Bại

**Lỗi:** `Không kết nối được EE`
```
→ Kiểm tra EE_URL và EE_TOKEN trong .env
→ Thử: curl -s http://<EE_URL>/api/v4/user -H "PRIVATE-TOKEN: <token>"
```

**Lỗi:** `errors=1` khi provision
```
→ Xem log: token thiếu permission, hoặc tên group/project bị trùng trên GitLab
→ Token cần scope: api (read/write)
```

### 14.2 Audit Service Không Nhận Webhook

**Kiểm tra service:**
```bash
# Health check
curl http://localhost:9000/health

# Test thủ công với đúng token
curl -X POST http://localhost:9000/webhook \
  -H "X-Gitlab-Token: <WEBHOOK_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{"event_name":"project_create","path":"test","path_with_namespace":"ocb/test","project_id":1}'
```

**Lỗi 403:** Token trong GitLab System Hook không khớp `WEBHOOK_SECRET` trong `.env`

**GitLab không gọi được đến service:**
```
→ Kiểm tra firewall: port 9000 phải accessible từ GitLab server
→ Local Docker: dùng IP gateway của Docker network (172.18.0.1), không dùng localhost
→ Kiểm tra: docker inspect gitlab-ee --format '{{range .NetworkSettings.Networks}}Gateway: {{.Gateway}}{{end}}'
```

### 14.3 Đọc / Debug Audit DB

```bash
# Xem nhanh toàn bộ findings
python scripts/audit_viewer.py

# Xem chỉ naming violations
python scripts/audit_viewer.py --violation naming

# Xem chỉ rogue repos
python scripts/audit_viewer.py --violation rogue

# Backup DB
cp data/audit.db data/audit_$(date +%Y%m%d).db
```

### 14.4 Quá Nhiều Rogue Items

Nếu audit báo nhiều rogue items sau khi chạy test:
```
→ Các items từ tests/valid/ là EXPECTED — test đã provision chúng
→ Rogue chỉ quan trọng trong production, nơi projects/ là nguồn chân lý
→ Có thể clean up bằng cách xóa groups/projects test trên GitLab
```

### 14.5 Docker GitLab Không Khởi Động

**Lỗi Puma port bind:**
```
→ external_url PHẢI là 'http://localhost' (không có port)
→ KHÔNG dùng 'http://localhost:8080' — sẽ gây xung đột port với Nginx
```

**Lỗi grafana config:**
```
→ Xóa dòng grafana['enable'] = false khỏi docker-compose.yml
→ Grafana option không còn được hỗ trợ trong GitLab CE mới
```

### 14.6 Python Dependencies

```bash
# Tạo venv và cài đặt
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# requirements.txt:
# requests>=2.31.0
# pyyaml>=6.0.1
# jsonschema>=4.21.1
# python-dotenv>=1.0.0
# (audit service dùng stdlib: sqlite3, smtplib, http.server, threading)
```
