"""Provision group/project trên GitLab EE và CE từ file YAML khai báo.

Cách dùng:
    # Provision tất cả file đã thay đổi so với commit trước (dùng trong CI)
    python scripts/provision.py --changed-only

    # Provision một hoặc nhiều file cụ thể
    python scripts/provision.py --files projects/UDTN/treasury.yml

    # Xem sẽ tạo gì mà không gọi API
    python scripts/provision.py --files projects/UDTN/treasury.yml --dry-run

    # Provision toàn bộ
    python scripts/provision.py --all
"""

import argparse
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
PROJECTS_DIR = ROOT / "projects"

load_dotenv(ROOT / ".env")

ROOT_GROUP = os.environ.get("ROOT_GROUP", "ocb")

logging.basicConfig(
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────── data model

@dataclass
class ProvisionPlan:
    """Kế hoạch tạo groups và projects cho một application."""
    department: str
    application: str
    groups: list[str] = field(default_factory=list)   # full paths của các group/subgroup
    projects: list[tuple[str, str]] = field(default_factory=list)  # (parent_group_path, project_path)


def build_plan(data: dict) -> ProvisionPlan:
    """Parse YAML data → ProvisionPlan."""
    dept = data["department"]
    app = data["application"]
    plan = ProvisionPlan(department=dept, application=app)

    rg = ROOT_GROUP
    # Group gốc tổ chức
    plan.groups.append(rg)
    # Group cấp 1: department
    plan.groups.append(f"{rg}/{dept}")
    # Subgroup cấp 2: application
    plan.groups.append(f"{rg}/{dept}/{app}")

    components = data.get("components") or {}

    # documents
    doc = components.get("documents") or {}
    if doc.get("enabled"):
        plan.projects.append((f"{rg}/{dept}/{app}", f"{app}-documents"))

    # backend
    backend = components.get("backend") or {}
    if backend.get("enabled"):
        backend_group = f"{rg}/{dept}/{app}/{app}-backend"
        plan.groups.append(backend_group)
        for svc in backend.get("services") or []:
            plan.projects.append((backend_group, svc))
        if backend.get("common_lib"):
            plan.projects.append((backend_group, f"{app}-common-lib"))

    # frontend
    frontend = components.get("frontend") or {}
    if frontend.get("enabled"):
        frontend_group = f"{rg}/{dept}/{app}/{app}-frontend"
        plan.groups.append(frontend_group)
        for app_name in frontend.get("apps") or []:
            plan.projects.append((frontend_group, app_name))
        if frontend.get("ui_libs"):
            plan.projects.append((frontend_group, f"{app}-ui-libs"))

    # tools, deployment, quality_security
    for component_key, suffix in [
        ("tools", "tools"),
        ("deployment", "deployment"),
        ("quality_security", "quality-security"),
    ]:
        comp = components.get(component_key) or {}
        if comp.get("enabled"):
            plan.projects.append((f"{rg}/{dept}/{app}", f"{app}-{suffix}"))

    return plan


# ─────────────────────────────────────────── dry-run printer

def print_plan(plan: ProvisionPlan):
    log.info("  Groups:")
    for g in plan.groups:
        log.info("    [GROUP]   %s", g)
    log.info("  Projects:")
    for parent, proj in plan.projects:
        log.info("    [PROJECT] %s/%s", parent, proj)


# ─────────────────────────────────────────── provision logic

def provision_plan(plan: ProvisionPlan, client, dry_run: bool) -> tuple[int, int, int]:
    """Thực thi plan trên một GitLab instance. Trả về (created, skipped, errors)."""
    created = skipped = errors = 0

    if dry_run:
        print_plan(plan)
        return 0, 0, 0

    # Tạo groups theo thứ tự (từ cấp 1 → cấp 3)
    group_cache: dict[str, dict] = {}
    for group_path in plan.groups:
        try:
            group = client.ensure_group_path(group_path)
            group_cache[group_path] = group
        except Exception as e:
            log.error("    ERROR group %s: %s", group_path, e)
            errors += 1

    # Tạo projects
    for parent_path, proj_path in plan.projects:
        parent = group_cache.get(parent_path)
        if parent is None:
            # Thử lấy lại từ API
            parent = client.get_group(parent_path)
        if parent is None:
            log.error("    ERROR project %s/%s: parent group không tồn tại", parent_path, proj_path)
            errors += 1
            continue
        try:
            full = f"{parent_path}/{proj_path}"
            existing = client.get_project(full)
            if existing:
                log.info("    SKIP  project %s (already exists)", full)
                skipped += 1
            else:
                client.create_project(
                    name=proj_path,
                    path=proj_path,
                    namespace_id=parent["id"],
                )
                created += 1
        except Exception as e:
            log.error("    ERROR project %s/%s: %s", parent_path, proj_path, e)
            errors += 1

    return created, skipped, errors


# ─────────────────────────────────────────── file helpers

def get_changed_files() -> list[Path]:
    """Lấy danh sách file YAML thay đổi trong commit hiện tại so với HEAD~1."""
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD", "--", "projects/"],
            cwd=ROOT,
            text=True,
        )
        paths = [ROOT / line.strip() for line in out.splitlines() if line.strip()]
        return [p for p in paths if p.suffix in (".yml", ".yaml") and p.exists()]
    except subprocess.CalledProcessError:
        log.warning("Không thể chạy git diff, fallback sang toàn bộ file")
        return list(PROJECTS_DIR.rglob("*.yml")) + list(PROJECTS_DIR.rglob("*.yaml"))


def collect_files(args) -> list[Path]:
    if args.all:
        return sorted(PROJECTS_DIR.rglob("*.yml")) + sorted(PROJECTS_DIR.rglob("*.yaml"))
    if args.changed_only:
        return get_changed_files()
    paths = []
    for f in args.files or []:
        p = Path(f)
        if not p.is_absolute():
            p = ROOT / p
        if p.exists():
            paths.append(p)
        else:
            log.warning("File không tồn tại, bỏ qua: %s", f)
    return paths


# ─────────────────────────────────────────── main

def main():
    parser = argparse.ArgumentParser(description="Provision GitLab groups/projects từ YAML")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--files", nargs="+", metavar="FILE")
    mode.add_argument("--changed-only", action="store_true")
    mode.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Chỉ in kế hoạch, không gọi API")
    args = parser.parse_args()

    files = collect_files(args)
    if not files:
        log.info("Không có file nào để provision.")
        sys.exit(0)

    if args.dry_run:
        log.info("=== DRY RUN MODE — không có thay đổi nào được thực hiện ===\n")
        for path in files:
            with open(path) as f:
                data = yaml.safe_load(f)
            plan = build_plan(data)
            log.info("File: %s  →  %s/%s", path, plan.department, plan.application)
            print_plan(plan)
            log.info("")
        sys.exit(0)

    # Load clients
    ee_url = os.environ.get("EE_URL", "").rstrip("/")
    ee_token = os.environ.get("EE_TOKEN", "")
    ce_url = os.environ.get("CE_URL", "").rstrip("/")
    ce_token = os.environ.get("CE_TOKEN", "")

    if not all([ee_url, ee_token, ce_url, ce_token]):
        log.error("Thiếu biến môi trường: EE_URL, EE_TOKEN, CE_URL, CE_TOKEN")
        sys.exit(1)

    from gitlab_client import GitLabClient
    ee = GitLabClient(ee_url, ee_token, label="EE")
    ce = GitLabClient(ce_url, ce_token, label="CE")

    # Kiểm tra kết nối
    if not ee.ping():
        log.error("Không kết nối được GitLab EE (%s)", ee_url)
        sys.exit(1)
    if not ce.ping():
        log.error("Không kết nối được GitLab CE (%s)", ce_url)
        sys.exit(1)

    log.info("")

    total_created = total_skipped = total_errors = 0

    for path in files:
        log.info("══ Processing: %s", path.relative_to(ROOT))
        with open(path) as f:
            data = yaml.safe_load(f)
        plan = build_plan(data)

        for client in [ee, ce]:
            log.info("  ── Target: %s (%s)", client.label, client.base.replace("/api/v4", ""))
            c, s, e = provision_plan(plan, client, dry_run=False)
            total_created += c
            total_skipped += s
            total_errors += e
            log.info("     created=%d  skipped=%d  errors=%d", c, s, e)

        log.info("")

    log.info("═══════════════════════════════════════")
    log.info("TOTAL  created=%d  skipped=%d  errors=%d", total_created, total_skipped, total_errors)

    sys.exit(1 if total_errors > 0 else 0)


if __name__ == "__main__":
    main()
