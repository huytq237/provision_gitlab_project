"""GitLab REST API client — tạo group/subgroup/project, idempotent."""

import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)


class GitLabClient:
    def __init__(self, url: str, token: str, label: str = "gitlab"):
        self.base = url.rstrip("/") + "/api/v4"
        self.session = requests.Session()
        self.session.headers.update({"PRIVATE-TOKEN": token})
        self.label = label

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        r = self.session.get(f"{self.base}{path}", params=params, timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, data: dict) -> dict:
        r = self.session.post(f"{self.base}{path}", json=data, timeout=30)
        r.raise_for_status()
        return r.json()

    def _put(self, path: str, data: dict) -> dict:
        r = self.session.put(f"{self.base}{path}", json=data, timeout=30)
        r.raise_for_status()
        return r.json()

    def _get_list(self, path: str, params: dict = None) -> list[dict]:
        """GET với auto-pagination qua X-Next-Page header."""
        params = dict(params or {})
        params.setdefault("per_page", 100)
        results = []
        page = 1
        while True:
            params["page"] = page
            r = self.session.get(f"{self.base}{path}", params=params, timeout=30)
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            results.extend(batch)
            next_page = r.headers.get("X-Next-Page", "")
            if not next_page:
                break
            page = int(next_page)
        return results

    # ------------------------------------------------------------------ groups

    def get_group(self, full_path: str) -> Optional[dict]:
        """Trả về group nếu tồn tại, None nếu không."""
        encoded = requests.utils.quote(full_path, safe="")
        return self._get(f"/groups/{encoded}")

    def create_group(
        self,
        name: str,
        path: str,
        parent_id: Optional[int] = None,
        visibility: str = "private",
    ) -> dict:
        """Tạo group hoặc subgroup. Idempotent: trả về group hiện có nếu đã tồn tại."""
        # Build full_path để kiểm tra trước
        if parent_id is not None:
            parent = self._get(f"/groups/{parent_id}")
            full_path = f"{parent['full_path']}/{path}"
        else:
            full_path = path

        existing = self.get_group(full_path)
        if existing:
            log.info("[%s] group already exists: %s", self.label, full_path)
            return existing

        payload = {"name": name, "path": path, "visibility": visibility}
        if parent_id is not None:
            payload["parent_id"] = parent_id

        group = self._post("/groups", payload)
        log.info("[%s] created group: %s", self.label, group["full_path"])
        return group

    def ensure_group_path(self, full_path: str, visibility: str = "private") -> dict:
        """Đảm bảo toàn bộ nested path tồn tại, tạo từng cấp nếu thiếu.

        full_path ví dụ: 'UDTN/treasury/treasury-backend'
        """
        parts = full_path.split("/")
        parent_id = None
        current_path = ""

        for part in parts:
            current_path = f"{current_path}/{part}" if current_path else part
            existing = self.get_group(current_path)
            if existing:
                parent_id = existing["id"]
            else:
                group = self.create_group(
                    name=part,
                    path=part,
                    parent_id=parent_id,
                    visibility=visibility,
                )
                parent_id = group["id"]

        return self._get(f"/groups/{requests.utils.quote(full_path, safe='')}")

    # ---------------------------------------------------------------- projects

    def get_project(self, full_path: str) -> Optional[dict]:
        encoded = requests.utils.quote(full_path, safe="")
        return self._get(f"/projects/{encoded}")

    def create_project(
        self,
        name: str,
        path: str,
        namespace_id: int,
        visibility: str = "private",
        initialize_with_readme: bool = True,
    ) -> dict:
        """Tạo project. Idempotent: trả về project hiện có nếu đã tồn tại."""
        # Tìm namespace full_path để build project path
        ns = self._get(f"/groups/{namespace_id}")
        if ns is None:
            # Có thể là user namespace
            ns = self._get(f"/namespaces/{namespace_id}")
        full_path = f"{ns['full_path']}/{path}" if ns else path

        existing = self.get_project(full_path)
        if existing:
            log.info("[%s] project already exists: %s", self.label, full_path)
            return existing

        payload = {
            "name": name,
            "path": path,
            "namespace_id": namespace_id,
            "visibility": visibility,
            "initialize_with_readme": initialize_with_readme,
        }
        project = self._post("/projects", payload)
        log.info("[%s] created project: %s", self.label, project["path_with_namespace"])
        return project

    # ------------------------------------------------------ list (audit support)

    def list_subgroups(self, group_id: int) -> list[dict]:
        return self._get_list(f"/groups/{group_id}/subgroups")

    def list_group_projects(self, group_id: int) -> list[dict]:
        return self._get_list(f"/groups/{group_id}/projects",
                              params={"include_subgroups": False})

    def list_descendant_groups(self, group_id: int) -> list[dict]:
        return self._get_list(f"/groups/{group_id}/descendant_groups")

    def rename_group(self, group_id: int, new_path: str, new_name: str) -> dict:
        result = self._put(f"/groups/{group_id}", {"path": new_path, "name": new_name})
        log.info("[%s] renamed group %d → %s", self.label, group_id, new_path)
        return result

    def rename_project(self, project_id: int, new_path: str, new_name: str) -> dict:
        result = self._put(f"/projects/{project_id}", {"path": new_path, "name": new_name})
        log.info("[%s] renamed project %d → %s", self.label, project_id, new_path)
        return result

    # ----------------------------------------------------------- health check

    def ping(self) -> bool:
        """Kiểm tra kết nối và token hợp lệ."""
        try:
            r = self.session.get(f"{self.base}/user", timeout=10)
            if r.status_code == 200:
                log.info("[%s] connected as: %s", self.label, r.json().get("username"))
                return True
            log.error("[%s] auth failed: HTTP %s", self.label, r.status_code)
            return False
        except requests.RequestException as e:
            log.error("[%s] connection error: %s", self.label, e)
            return False
