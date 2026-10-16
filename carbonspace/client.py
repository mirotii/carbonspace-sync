"""Thin wrapper around the CarbonSpace Web API.

Reads work with the API key; writes need the web access token (the API key is
rejected with 401 on every write endpoint, see docs/api-notes.md).
"""
from __future__ import annotations

import re
from urllib.parse import quote

import requests

BASE_URL = "https://api.public.carbonspace.earth"
EMPTY_GUID = "00000000-0000-0000-0000-000000000000"


class CarbonSpaceError(Exception):
    pass


def strip_html(text: str | None) -> str:
    """Search results wrap matches in <b>…</b>."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


class CarbonSpaceClient:
    def __init__(self, api_key: str | None = None, token: str | None = None,
                 base_url: str = BASE_URL, timeout: float = 60):
        self.api_key = api_key
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self._components: dict[int, dict | None] = {}

    # ---- transport -------------------------------------------------------
    def _headers(self, use_token: bool) -> dict:
        h = {"accept": "application/json"}
        if use_token:
            if not self.token:
                raise CarbonSpaceError("This needs a login token. Run:  cs_sync login")
            h["Authorization"] = f"Bearer {self.token}"
        elif self.api_key:
            h["X-API-KEY"] = self.api_key
        else:
            raise CarbonSpaceError("No credentials. Run:  cs_sync setup")
        return h

    def _request(self, method: str, path: str, *, write: bool = False, json=None, params=None):
        url = self.base_url + path
        # Reads use the long-lived API key; writes need the token. Some read
        # endpoints (e.g. Component/GetByProject) also reject the API key, so a
        # 401 on a read is retried with the token when one is available.
        use_token = write or not self.api_key
        try:
            r = self.session.request(method, url, headers=self._headers(use_token), json=json,
                                     params=params, timeout=self.timeout)
            if r.status_code == 401 and not use_token and self.token:
                use_token = True
                r = self.session.request(method, url, headers=self._headers(True), json=json,
                                         params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise CarbonSpaceError(f"Network error calling CarbonSpace ({method} {path}): {e}") from e
        if r.ok:
            if not r.content:
                return None
            try:
                return r.json()
            except ValueError:
                return r.text
        detail = r.text[:500]
        try:
            body = r.json()
            detail = body.get("Message") or body.get("title") or detail
        except ValueError:
            pass
        if r.status_code == 401:
            msg = ("401 Unauthorized - the login token is missing or expired. Run:  cs_sync login"
                   if use_token else
                   f"401 Unauthorized on {path}. This part of the API may need a login token. Run:  cs_sync login")
        elif r.status_code == 403:
            msg = f"403 Forbidden - your account has no access to this item. ({detail})"
        elif r.status_code == 404:
            msg = f"404 Not found - {method} {path} ({detail})"
        elif r.status_code == 400:
            msg = f"400 Bad request - server said: {detail}"
        else:
            msg = f"HTTP {r.status_code} on {method} {path}: {detail}"
        raise CarbonSpaceError(msg)

    def get(self, path, **kw):
        return self._request("GET", path, **kw)

    # ---- reads -----------------------------------------------------------
    def get_me(self) -> dict:
        return self.get("/api/User/GetMe")

    def projects(self) -> list[dict]:
        return self.get("/api/Project/GetAll")

    def component(self, component_id: int) -> dict | None:
        """Component by id (cached). Returns None if it does not exist / no access."""
        if component_id not in self._components:
            try:
                c = self.get(f"/api/Component/GetById/{component_id}")
            except CarbonSpaceError as e:
                if str(e).startswith(("404", "403")) or "not found" in str(e).lower():
                    c = None
                else:
                    raise
            self._components[component_id] = c or None
        return self._components[component_id]

    def components_by_project(self, project_id: int) -> list[dict]:
        comps = self.get(f"/api/Component/GetByProject/{project_id}") or []
        for c in comps:
            self._components.setdefault(c["Id"], c)
        return comps

    def search_components(self, query: str, project_ids: list[int] | None = None) -> list[dict]:
        params = [("projects", p) for p in project_ids or []]
        res = self.get(f"/api/Search/SearchComponentByText/{quote(query, safe='')}", params=params) or []
        for r in res:
            r["Name"] = strip_html(r.get("Name"))
            r["Path"] = strip_html(r.get("Path"))
        return res

    def calculations(self, project_id: int) -> list[dict]:
        return self.get(f"/api/Calculation/GetByProject/{project_id}") or []

    def calculation_set(self, calculation_id: int) -> dict:
        return self.get(f"/api/Calculation/GetCalculationSetById/{calculation_id}")

    def bom(self, calculation_id: int) -> dict:
        return self.get(f"/api/Bom/GetByCalculation/{calculation_id}")

    # ---- writes (token required) -----------------------------------------
    def create_calculation(self, project_id: int, name: str) -> dict:
        """New calculation with CarbonSpace's default shearing-layer groups."""
        return self._request("POST", "/api/Calculation/CreateOrUpdateCalculation", write=True,
                             json={"Name": name, "ProjectId": project_id, "CarbonQuantities": []})

    def copy_calculation(self, calculation_id: int, project_id: int, name: str) -> dict:
        return self._request("POST", f"/api/Calculation/Copy/{calculation_id}", write=True,
                             params={"targetProject": project_id, "name": name})

    def create_group(self, calculation_id: int, project_id: int, name: str,
                     parent_id: int | None = None, ordinal: int = 99) -> dict:
        return self._request("POST", "/api/CalculationGroup/Create", write=True, json={
            "Id": 0, "ObjectId": EMPTY_GUID, "CalculationId": calculation_id, "Name": name,
            "ParentId": parent_id, "ReplacementFactor": 1, "IsComparison": False,
            "Ordinal": ordinal, "ProjectId": project_id,
        })

    def create_line(self, calculation_id: int, project_id: int, group_id: int,
                    component_id: int, expression: str, comment: str | None = None) -> None:
        self._request("POST", "/api/CalculationGroupComponent/Create", write=True, json={
            "Id": 0, "ObjectId": EMPTY_GUID, "Quantity": 0, "Expression": expression,
            "Comment": comment, "CalculationGroupId": group_id, "ComponentId": component_id,
            "CalculationId": calculation_id, "Ordinal": 0, "Suppressed": False,
            "ProjectId": project_id,
        })

    def update_line(self, line: dict, **fields) -> None:
        """PATCH selected fields of a calculation line (e.g. Expression, Comment).
        Note: 'Quantity' is not an allowed field; the server derives it from Expression."""
        body = {**line, **fields}
        for k in ("Component", "Calculation", "CalculationGroup"):
            body.pop(k, None)
        self._request("PATCH", "/api/CalculationGroupComponent/Update", write=True,
                      params=[("fields", f) for f in fields], json=body)

    def delete_line(self, line_id: int) -> None:
        self._request("DELETE", f"/api/CalculationGroupComponent/Delete/{line_id}", write=True)

    def update_inventory(self, calculation_id: int, values: dict[str, str]) -> None:
        """Also forces the server to re-evaluate every line expression."""
        self._request("PATCH",
                      f"/api/Calculation/UpdateInventoryDictionary/UpdateInventoryDictionary/{calculation_id}",
                      write=True, json=values)
