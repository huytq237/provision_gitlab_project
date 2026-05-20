"""Audit check functions — stateless, không có I/O ngoài logging.

Dùng chung cho webhook handler và polling scanner trong audit_service.py.
"""

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
PROJECTS_DIR = ROOT / "projects"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")

log = logging.getLogger(__name__)


@dataclass
class NamingIssue:
    kind: str           # "group" | "project"
    id: int
    full_path: str
    current_path: str   # path segment cuối (không có slash)
    suggested_path: str
    instance: str       # "EE" | "CE"


@dataclass
class RogueItem:
    kind: str           # "group" | "project"
    id: int
    full_path: str
    instance: str       # "EE" | "CE"


def slugify(s: str) -> str:
    """Convert string thành valid slug.

    Các bước:
    1. lowercase
    2. space, underscore, dot → '-'
    3. xóa ký tự không phải [a-z0-9-]
    4. collapse '--+' → '-'
    5. strip leading/trailing '-'
    """
    s = s.lower()
    s = re.sub(r"[ _.]", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


def check_naming_single(
    path_segment: str,
    full_path: str,
    kind: str,
    item_id: int,
    instance: str,
) -> "NamingIssue | None":
    """Kiểm tra naming của một group/project. Trả về NamingIssue nếu vi phạm, None nếu OK."""
    if SLUG_RE.match(path_segment):
        return None
    return NamingIssue(
        kind=kind,
        id=item_id,
        full_path=full_path,
        current_path=path_segment,
        suggested_path=slugify(path_segment),
        instance=instance,
    )


def build_expected_set(projects_dir: Path) -> tuple[set[str], set[str]]:
    """Đọc tất cả YAML trong projects_dir → (expected_groups, expected_projects).

    expected_groups: full_path strings, e.g. {'ocb', 'ocb/udtn', 'ocb/udtn/treasury', ...}
    expected_projects: full_path strings, e.g. {'ocb/udtn/treasury/treasury-documents', ...}

    Import build_plan bên trong để tránh circular dependency khi audit_service load module này.
    """
    sys.path.insert(0, str(Path(__file__).parent))
    from provision import build_plan  # noqa: PLC0415

    expected_groups: set[str] = set()
    expected_projects: set[str] = set()

    yaml_files = sorted(projects_dir.rglob("*.yml")) + sorted(projects_dir.rglob("*.yaml"))
    for yaml_file in yaml_files:
        try:
            with open(yaml_file) as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                continue
            plan = build_plan(data)
            expected_groups.update(plan.groups)
            for parent_path, proj_name in plan.projects:
                expected_projects.add(f"{parent_path}/{proj_name}")
        except Exception as e:
            log.warning("Bỏ qua file %s: %s", yaml_file, e)

    return expected_groups, expected_projects


def traverse_gitlab(client, root_group: str) -> tuple[list[dict], list[dict]]:
    """Traverse toàn bộ cây group/project dưới root_group.

    Trả về (all_groups, all_projects).
    Raise RuntimeError nếu root_group không tồn tại.
    """
    root = client.get_group(root_group)
    if root is None:
        raise RuntimeError(f"ROOT_GROUP '{root_group}' không tồn tại trên {client.label}")

    all_groups = [root] + client.list_descendant_groups(root["id"])

    all_projects: list[dict] = []
    for g in all_groups:
        all_projects.extend(client.list_group_projects(g["id"]))

    return all_groups, all_projects
