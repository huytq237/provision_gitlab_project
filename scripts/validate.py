"""Validate các file YAML project trước khi provision.

Dùng trong CI pipeline (MR stage) hoặc chạy tay:
    python scripts/validate.py --files projects/UDTN/treasury.yml
    python scripts/validate.py --all                  # validate toàn bộ
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path

import yaml
import jsonschema

ROOT = Path(__file__).parent.parent
SCHEMA_PATH = ROOT / "schema" / "project_schema.json"
PROJECTS_DIR = ROOT / "projects"

logging.basicConfig(format="%(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


def load_schema() -> dict:
    with open(SCHEMA_PATH) as f:
        return json.load(f)


def validate_file(path: Path, schema: dict, seen: dict) -> list[str]:
    """Validate một file YAML. Trả về list lỗi (rỗng = OK)."""
    errors = []

    # 1. YAML syntax
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return [f"YAML syntax error: {e}"]

    if not isinstance(data, dict):
        return ["File phải là một YAML mapping (dict)"]

    # 2. JSON Schema
    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        errors.append(f"Schema error: {e.message} (path: {' -> '.join(str(p) for p in e.absolute_path)})")
        # Không return sớm — tiếp tục check naming nếu có thể

    # 3. Naming convention
    for field in ("department", "application"):
        val = data.get(field, "")
        if val and not SLUG_RE.match(str(val)):
            errors.append(
                f"'{field}' phải chỉ gồm chữ thường, số, dấu gạch ngang (-), "
                f"không bắt đầu hoặc kết thúc bằng '-'. Got: '{val}'"
            )

    components = data.get("components", {}) or {}

    # Validate tên service
    backend = components.get("backend", {}) or {}
    for svc in backend.get("services", []) or []:
        if not SLUG_RE.match(str(svc)):
            errors.append(f"backend.services: tên service không hợp lệ: '{svc}'")

    # Validate tên app frontend
    frontend = components.get("frontend", {}) or {}
    for app in frontend.get("apps", []) or []:
        if not SLUG_RE.match(str(app)):
            errors.append(f"frontend.apps: tên app không hợp lệ: '{app}'")

    # 4. Duplicate check
    dept = str(data.get("department", "")).lower()
    app = str(data.get("application", "")).lower()
    key = f"{dept}/{app}"
    if key in seen:
        errors.append(
            f"Duplicate: '{key}' đã được khai báo trong {seen[key]}. "
            "Mỗi department/application chỉ được khai báo một lần."
        )
    else:
        seen[key] = str(path)

    return errors


def collect_files(args_files: list[str], all_mode: bool) -> list[Path]:
    if all_mode:
        return sorted(PROJECTS_DIR.rglob("*.yml")) + sorted(PROJECTS_DIR.rglob("*.yaml"))

    paths = []
    for f in args_files:
        p = Path(f)
        if not p.exists():
            log.warning("File không tồn tại, bỏ qua: %s", f)
            continue
        paths.append(p)
    return paths


def main():
    parser = argparse.ArgumentParser(description="Validate YAML project files")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--files", nargs="+", metavar="FILE", help="Danh sách file cần validate")
    group.add_argument("--all", action="store_true", help="Validate toàn bộ file trong projects/")
    args = parser.parse_args()

    schema = load_schema()
    files = collect_files(getattr(args, "files", None) or [], args.all)

    if not files:
        log.info("Không có file YAML nào để validate.")
        sys.exit(0)

    seen: dict[str, str] = {}
    total = len(files)
    passed = 0
    failed = 0

    log.info("Validating %d file(s)...\n", total)

    for path in files:
        errs = validate_file(path, schema, seen)
        if errs:
            failed += 1
            log.error("FAIL  %s", path)
            for e in errs:
                log.error("      ✗ %s", e)
        else:
            passed += 1
            log.info("PASS  %s", path)

    log.info("\n─────────────────────────────────")
    log.info("Result: %d passed, %d failed (total %d)", passed, failed, total)

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
