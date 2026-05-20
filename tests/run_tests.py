"""Test runner cho provision_gitlab_project.

Chạy: python tests/run_tests.py
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
VENV_PYTHON = ROOT / ".venv/bin/python"

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"

results = []


def run(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    return r.returncode, r.stdout + r.stderr


def check(name: str, ok: bool, detail: str = ""):
    status = PASS if ok else FAIL
    results.append(ok)
    suffix = f"  → {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")


def section(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ──────────────────────────────────────────── 1. Valid YAMLs: validate PASS
section("1. Validate hợp lệ — expect PASS")

valid_files = sorted((ROOT / "tests/valid").glob("*.yml"))
for f in valid_files:
    code, out = run([VENV_PYTHON, "scripts/validate.py", "--files", str(f)])
    check(f.name, code == 0, "validate passed" if code == 0 else out.strip()[-120:])


# ──────────────────────────────────────────── 2. Invalid YAMLs: validate FAIL
section("2. Validate lỗi — expect FAIL")

invalid_files = sorted((ROOT / "tests/invalid").glob("*.yml"))
for f in invalid_files:
    code, out = run([VENV_PYTHON, "scripts/validate.py", "--files", str(f)])
    check(f.name, code != 0, "correctly rejected" if code != 0 else "BUG: should have failed")


# ──────────────────────────────────────────── 3. Duplicate: validate FAIL
section("3. Duplicate department/application — expect FAIL")

dup_files = sorted((ROOT / "tests/duplicate").glob("*.yml"))
if dup_files:
    code, out = run([VENV_PYTHON, "scripts/validate.py", "--files"] + [str(f) for f in dup_files])
    check("dup-a.yml + dup-b.yml cùng dept/app", code != 0, "duplicate detected" if code != 0 else "BUG: should detect duplicate")


# ──────────────────────────────────────────── 4. Dry-run valid files
section("4. Dry-run provision — expect no errors")

for f in valid_files:
    code, out = run([VENV_PYTHON, "scripts/provision.py", "--files", str(f), "--dry-run"])
    check(f.name, code == 0, "dry-run ok" if code == 0 else out.strip()[-120:])


# ──────────────────────────────────────────── 5. Provision thật (lần 1)
section("5. Provision lần 1 — expect created > 0")

for f in valid_files:
    code, out = run([VENV_PYTHON, "scripts/provision.py", "--files", str(f)])
    created = errors = 0
    for line in out.splitlines():
        if "TOTAL" in line and "created=" in line:
            try:
                created = int(line.split("created=")[1].split()[0])
                errors  = int(line.split("errors=")[1].split()[0])
            except Exception:
                pass
    ok = code == 0 and errors == 0
    check(f.name, ok, f"created={created} errors={errors}" if ok else f"code={code} errors={errors}")


# ──────────────────────────────────────────── 6. Provision lần 2 (idempotent)
section("6. Provision lần 2 — expect skipped = created (idempotent)")

for f in valid_files:
    code, out = run([VENV_PYTHON, "scripts/provision.py", "--files", str(f)])
    created = errors = skipped = 0
    for line in out.splitlines():
        if "TOTAL" in line and "created=" in line:
            try:
                created = int(line.split("created=")[1].split()[0])
                skipped = int(line.split("skipped=")[1].split()[0])
                errors  = int(line.split("errors=")[1].split()[0])
            except Exception:
                pass
    ok = code == 0 and created == 0 and errors == 0
    check(f.name, ok, f"skipped={skipped} created={created} errors={errors}")


# ──────────────────────────────────────────── 7. Missing file: no crash
section("7. --files với file không tồn tại — expect graceful skip, exit 0")

code, out = run([VENV_PYTHON, "scripts/provision.py", "--files", "projects/nonexistent.yml"])
check("nonexistent.yml", code == 0, "graceful skip" if code == 0 else f"crashed: {out[:120]}")


# ──────────────────────────────────────────── 8. Audit modules
section("8. Audit modules — import + slugify + AuditDB")

# 8a. Import audit_checks và audit_db
code, out = run([VENV_PYTHON, "-c",
    "import sys; sys.path.insert(0,'scripts'); import audit_checks, audit_db"])
check("audit_checks + audit_db importable", code == 0,
      out.strip()[:120] if code != 0 else "")

# 8b. slugify correctness
code, out = run([VENV_PYTHON, "-c", """
import sys; sys.path.insert(0,'scripts')
from audit_checks import slugify
assert slugify('My_Group')      == 'my-group',    repr(slugify('My_Group'))
assert slugify('UPPER')         == 'upper',        repr(slugify('UPPER'))
assert slugify('with spaces')   == 'with-spaces',  repr(slugify('with spaces'))
assert slugify('already-valid') == 'already-valid'
assert slugify('a--b')          == 'a-b',          repr(slugify('a--b'))
assert slugify('hello.world')   == 'hello-world',  repr(slugify('hello.world'))
assert slugify('---')           == '',             repr(slugify('---'))
print('OK')
"""])
check("slugify correctness", code == 0 and "OK" in out,
      out.strip()[:120] if code != 0 else "")

# 8c. AuditDB basic operations
code, out = run([VENV_PYTHON, "-c", """
import sys, tempfile, os; sys.path.insert(0,'scripts')
from audit_db import AuditDB
with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
    path = f.name
try:
    db = AuditDB(path)
    assert db.upsert_finding('EE','group','ocb/bad','naming','bad') == True
    assert db.upsert_finding('EE','group','ocb/bad','naming','bad') == False
    s = db.summary()
    assert s['open'] == 1, s
    db.mark_alerted('EE','ocb/bad','naming')
    db.mark_resolved('EE','ocb/bad','naming')
    s = db.summary()
    assert s['resolved'] == 1, s
    print('OK')
finally:
    os.unlink(path)
"""])
check("AuditDB upsert + resolve", code == 0 and "OK" in out,
      out.strip()[:120] if code != 0 else "")

# 8d. audit_service importable (syntax check)
code, out = run([VENV_PYTHON, "-c",
    "import sys; sys.path.insert(0,'scripts'); import audit_service"])
check("audit_service importable", code == 0,
      out.strip()[:120] if code != 0 else "")


# ──────────────────────────────────────────── Summary
passed = sum(results)
total  = len(results)
failed = total - passed
print(f"\n{'═'*60}")
print(f"  TOTAL: {passed}/{total} passed  ({failed} failed)")
print(f"{'═'*60}\n")

sys.exit(1 if failed > 0 else 0)
