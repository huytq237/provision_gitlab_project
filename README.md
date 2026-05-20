# GitLab Provisioner & Audit Service

GitOps tool tự động tạo groups/projects trên **2 GitLab instance** (EE + CE) từ file YAML, kèm daemon giám sát naming convention và phát hiện repo tạo chui.

---

## Tính Năng

| Tính năng | Mô tả |
|---|---|
| **GitOps Provisioning** | Khai báo cấu trúc GitLab bằng YAML → merge vào `main` → tự động tạo |
| **Idempotent** | Chạy lại bất kỳ lần nào cũng không tạo duplicate |
| **Dual Instance** | Sync đồng thời lên cả GitLab EE và GitLab CE |
| **YAML Validation** | JSON Schema + naming convention + duplicate check (chạy trong CI MR) |
| **Audit Service** | Daemon real-time: webhook + polling phát hiện vi phạm naming và repo tạo chui |
| **SQLite Storage** | Lưu lịch sử findings, tránh spam alert cho cùng 1 vi phạm |
| **Email Alert** | Gửi SMTP khi phát hiện vi phạm mới |
| **Audit Viewer** | CLI xem nội dung `audit.db` trực tiếp trên terminal |

---

## Cấu Trúc Group Được Tạo

```
ocb/                              ← ROOT_GROUP
  {department}/                   ← Group cấp 1
    {application}/                ← Subgroup cấp 2
      {app}-documents             ← Project tài liệu
      {app}-backend/              ← Subgroup backend
        {service-1}
        {service-2}
        {app}-common-lib
      {app}-frontend/             ← Subgroup frontend
        {app-name}
        {app}-ui-libs
      {app}-tools
      {app}-deployment
      {app}-quality-security
```

---

## Cài Đặt

```bash
git clone https://github.com/huytq237/provision_gitlab_project.git
cd provision_gitlab_project

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# Chỉnh sửa .env với URL và tokens của GitLab EE/CE
```

---

## Khai Báo Project (YAML)

Tạo file trong `projects/<DEPARTMENT>/<application>.yml`:

```yaml
department: udtn
application: treasury

components:
  documents:
    enabled: true

  backend:
    enabled: true
    services:
      - treasury-core
      - treasury-reporting
    common_lib: true

  frontend:
    enabled: true
    apps:
      - treasury-web

  deployment:
    enabled: true
  quality_security:
    enabled: true
```

**Quy tắc đặt tên:** chỉ chữ thường, số, dấu `-` — không bắt đầu/kết thúc bằng `-`, tối thiểu 2 ký tự.

---

## Sử Dụng

### Validate YAML

```bash
# Validate file cụ thể
python scripts/validate.py --files projects/UDTN/treasury.yml

# Validate toàn bộ
python scripts/validate.py --all
```

### Provision

```bash
# Preview (không gọi API)
python scripts/provision.py --files projects/UDTN/treasury.yml --dry-run

# Provision file cụ thể
python scripts/provision.py --files projects/UDTN/treasury.yml

# Provision toàn bộ
python scripts/provision.py --all
```

### Audit Service

```bash
# Chạy daemon (webhook + polling)
python scripts/audit_service.py

# One-shot scan (dùng trong CI hoặc kiểm tra thủ công)
python scripts/audit_service.py --once --mode all
python scripts/audit_service.py --once --mode naming --alert-email ops@example.com
python scripts/audit_service.py --once --mode rogue
```

### Xem Audit Database

```bash
# Tóm tắt + danh sách findings đang open
python scripts/audit_viewer.py

# Lọc theo loại vi phạm hoặc instance
python scripts/audit_viewer.py --violation naming
python scripts/audit_viewer.py --violation rogue --instance EE
python scripts/audit_viewer.py --all   # kể cả đã resolved
```

---

## Cấu Hình `.env`

```bash
# GitLab instances
ROOT_GROUP=ocb
EE_URL=https://gitlab-dso.ocb.vn
EE_TOKEN=glpat-...
CE_URL=https://git.ocb.vn
CE_TOKEN=glpat-...

# Audit Service
WEBHOOK_PORT=9000
WEBHOOK_SECRET=your-random-secret
SCAN_INTERVAL_MINUTES=60
AUDIT_DB_PATH=data/audit.db

# Email Alert
ALERT_EMAIL=ops@example.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_TLS=true
SMTP_USER=
SMTP_PASS=
```

---

## CI/CD Pipeline

`.gitlab-ci.yml` định nghĩa 3 stages:

```
validate  →  provision  →  audit
```

| Job | Trigger | Mô tả |
|---|---|---|
| `validate:yaml` | Mỗi MR | Validate file YAML thay đổi trong MR |
| `validate:all` | Push main | Validate toàn bộ `projects/` |
| `provision:changed` | Push main | Provision file thay đổi |
| `provision:all` | Manual | Full resync |
| `audit:naming` | Schedule / Manual | Kiểm tra naming convention |
| `audit:rogue` | Schedule / Manual | Phát hiện rogue repos |
| `audit:all` | Schedule / Manual | Kiểm tra toàn diện |

**CI Variables cần cấu hình** (Settings → CI/CD → Variables):
`EE_URL`, `EE_TOKEN`, `CE_URL`, `CE_TOKEN`, `ROOT_GROUP`, `ALERT_EMAIL`

---

## Audit Service — Cách Hoạt Động

```
GitLab tạo/đổi tên group hoặc project
        │
        ▼ System Hook (POST /webhook)
  ┌─────────────────────────────┐
  │     Audit Service :9000     │
  │                             │
  │  Webhook    +   Polling     │
  │  (real-time)    (60 phút)   │
  │         │           │       │
  │         ▼           ▼       │
  │     audit_checks.py         │
  │   ┌─────────────────────┐   │
  │   │  Naming check       │   │
  │   │  SLUG_RE validation │   │
  │   │                     │   │
  │   │  Rogue check        │   │
  │   │  actual vs YAML     │   │
  │   └──────────┬──────────┘   │
  │              ▼              │
  │       SQLite (audit.db)     │
  │       Email Alert (SMTP)    │
  └─────────────────────────────┘
```

**Cấu hình GitLab System Hook:**
- Vào Admin Area → System Hooks
- URL: `http://<server>:9000/webhook`
- Secret Token: giá trị `WEBHOOK_SECRET`
- Không cần tích thêm checkbox

---

## Deploy Systemd (Production)

```bash
sudo cp systemd/gitlab-audit.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gitlab-audit

# Theo dõi log
journalctl -u gitlab-audit -f
```

---

## Test

```bash
# 35 provisioning test cases
.venv/bin/python tests/run_tests.py

# 30 webhook test cases (cần audit_service đang chạy)
python scripts/audit_service.py &
.venv/bin/python tests/test_webhook.py
```

| Test Suite | Cases | Cần GitLab |
|---|---|---|
| `run_tests.py` | 35 | Có (section 5-6) |
| `test_webhook.py` | 30 | Có (section 8) |

---

## Môi Trường Local (Docker)

```bash
# Khởi động 2 GitLab CE containers (port 8080 và 8081)
docker compose up -d

# Lấy root password
docker exec gitlab-ee grep 'Password:' /etc/gitlab/initial_root_password

# Tạo access token tự động
docker exec gitlab-ee gitlab-rails runner "
  user = User.find_by_username('root')
  token = user.personal_access_tokens.create!(
    name: 'provision-token', scopes: ['api'], expires_at: 1.year.from_now)
  puts token.token
"
```

---

## Tài Liệu

Xem [TECHNICAL.md](TECHNICAL.md) để biết chi tiết kiến trúc, tất cả API methods, schema database, cấu hình đầy đủ và hướng dẫn xử lý sự cố.
