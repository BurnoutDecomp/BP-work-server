from __future__ import annotations

import asyncio

from bp_work_server.decomp import DecompRepo
from bp_work_server.github import login_from_noreply_email
from bp_work_server.store import WorkStore


async def repo_revision(decomp: DecompRepo) -> str | None:
    if not hasattr(decomp, "revision"):
        return None
    return await asyncio.to_thread(decomp.revision)


class AttributionService:
    def __init__(self, store: WorkStore, decomp: DecompRepo):
        self.store = store
        self.decomp = decomp

    async def file_attrs(self, dest_paths: set[str]) -> dict[str, dict]:
        if not dest_paths:
            return {}
        repo_rev = await repo_revision(self.decomp)

        def resolve_histories() -> dict[str, dict]:
            histories: dict[str, dict] = {}
            for dest_path in dest_paths:
                if repo_rev:
                    cached = self.store.attribution_cache_get(
                        scope="file", dest_path=dest_path, repo_rev=repo_rev
                    )
                    if cached is not None:
                        histories[dest_path] = cached
                        continue
                history = self.decomp.history(dest_path)
                contributors = (
                    self.decomp.contributors(dest_path)
                    if hasattr(self.decomp, "contributors")
                    else {"contributors": [], "basis": "surviving_lines", "path": None}
                )
                data = {
                    "latest": history[0] if history else None,
                    "contributors": contributors,
                }
                histories[dest_path] = data
                if repo_rev:
                    self.store.attribution_cache_set(
                        scope="file",
                        dest_path=dest_path,
                        repo_rev=repo_rev,
                        payload=data,
                    )
            return histories

        histories = await asyncio.to_thread(resolve_histories)
        if not histories or not any(
            data.get("latest") or (data.get("contributors", {}).get("contributors") or [])
            for data in histories.values()
        ):
            return {}

        aliases, profiles = await asyncio.to_thread(self.store.actor_maps)
        login_map: dict[str, str] = {}
        return {
            dest_path: attribute_file_data(data, login_map, aliases, profiles)
            for dest_path, data in histories.items()
        }

    async def function_attrs(self, items: list[dict]) -> None:
        pairs = [
            (item.get("tu_dest_path") or item.get("dest_path"), item.get("name"))
            for item in items
            if item.get("status") != "todo"
            and (item.get("tu_dest_path") or item.get("dest_path"))
            and item.get("name")
        ]
        if not pairs:
            return
        repo_rev = await repo_revision(self.decomp)

        def resolve() -> dict[tuple[str, str], dict]:
            out: dict[tuple[str, str], dict] = {}
            for dest_path, name in pairs:
                if repo_rev:
                    cached = self.store.attribution_cache_get(
                        scope="function",
                        dest_path=dest_path,
                        function_name=name,
                        repo_rev=repo_rev,
                    )
                    if cached is not None:
                        out[(dest_path, name)] = cached
                        continue
                if hasattr(self.decomp, "function_contributors"):
                    data = self.decomp.function_contributors(dest_path, name)
                elif hasattr(self.decomp, "contributors"):
                    data = self.decomp.contributors(dest_path)
                else:
                    data = {}
                out[(dest_path, name)] = data
                if repo_rev:
                    self.store.attribution_cache_set(
                        scope="function",
                        dest_path=dest_path,
                        function_name=name,
                        repo_rev=repo_rev,
                        payload=data,
                    )
            return out

        raw = await asyncio.to_thread(resolve)
        if not raw or not any((item.get("contributors") or []) for item in raw.values()):
            return
        login_map: dict[str, str] = {}
        aliases, profiles = await asyncio.to_thread(self.store.actor_maps)
        for item in items:
            dest_path = item.get("tu_dest_path") or item.get("dest_path")
            name = item.get("name")
            attr = attribute_contributor_data(raw.get((dest_path, name)), login_map, aliases, profiles)
            if not attr:
                continue
            for key in (
                "contributors",
                "contributor_count",
                "primary_contributor",
                "primary_contributor_login",
                "primary_contributor_lines",
                "primary_contributor_percent",
                "attribution_basis",
                "line_range",
                "function_range_found",
            ):
                if key in attr:
                    item[key] = attr[key]
            if attr.get("primary_contributor"):
                item["completed_by"] = attr["primary_contributor"]
                item["completed_by_login"] = attr.get("primary_contributor_login")


def apply_tu_file_attr(item: dict, attr: dict | None) -> None:
    if not attr:
        return
    if item.get("status") == "todo":
        return
    if attr.get("latest_change_at"):
        item["updated_at"] = attr["latest_change_at"]
    for key in (
        "contributors",
        "contributor_count",
        "primary_contributor",
        "primary_contributor_login",
        "primary_contributor_lines",
        "primary_contributor_percent",
        "attribution_basis",
        "latest_change_by",
        "latest_change_by_login",
        "latest_change_at",
    ):
        if key in attr:
            item[key] = attr[key]
    primary = attr.get("primary_contributor") or attr.get("latest_change_by")
    if primary:
        item["completed_by"] = primary
        item["completed_by_login"] = (
            attr.get("primary_contributor_login") or attr.get("latest_change_by_login")
        )


def hide_import_timestamp_for_idle_todo(item: dict) -> None:
    if (
        item.get("status") == "todo"
        and not item.get("owner")
        and not item.get("completed_by")
        and not item.get("primary_contributor")
        and not item.get("last_actor")
    ):
        item["updated_at"] = None


def attribute_commit(
    commit: dict,
    login_map: dict[str, str],
    aliases: dict[str, str],
    profiles: dict[str, str] | None = None,
) -> dict:
    author, login = attribute_identity(
        commit.get("name"), commit.get("email"), login_map, aliases, profiles or {}
    )
    return {"date": commit["date"], "author": author, "login": login}


def attribute_identity(
    name: str | None,
    email: str | None,
    login_map: dict[str, str],
    aliases: dict[str, str],
    profiles: dict[str, str],
) -> tuple[str | None, str | None]:
    email = email or ""
    login = login_map.get(email.lower()) or login_from_noreply_email(email)
    candidates = [login, email, name]
    for candidate in candidates:
        cleaned = str(candidate).strip() if candidate is not None else ""
        if not cleaned:
            continue
        author = aliases.get(cleaned.lower())
        if author:
            return author, profiles.get(author) or login
        if candidate == name:
            return cleaned, profiles.get(cleaned) or login
    fallback = login or (email.strip() or None)
    return fallback, profiles.get(fallback or "") or login


def attribute_contributor_data(
    raw: dict | None,
    login_map: dict[str, str],
    aliases: dict[str, str],
    profiles: dict[str, str],
) -> dict:
    if not raw:
        return {}
    grouped: dict[str, dict] = {}
    total = 0
    for contributor in raw.get("contributors") or []:
        author, login = attribute_identity(
            contributor.get("name"), contributor.get("email"), login_map, aliases, profiles
        )
        if not author:
            continue
        lines = int(contributor.get("lines") or 0)
        if lines <= 0:
            continue
        current = grouped.setdefault(author, {"author": author, "login": login, "lines": 0})
        current["lines"] += lines
        if login:
            current["login"] = login
        total += lines
    contributors = sorted(grouped.values(), key=lambda item: (-item["lines"], item["author"]))
    for contributor in contributors:
        contributor["percent"] = round((contributor["lines"] / total) * 100, 1) if total else 0
    primary = contributors[0] if contributors else None
    result = {
        "contributors": contributors,
        "contributor_count": len(contributors),
        "attribution_basis": raw.get("basis") or "surviving_lines",
        "line_range": raw.get("line_range"),
    }
    if "function_range_found" in raw:
        result["function_range_found"] = bool(raw.get("function_range_found"))
    if primary:
        result.update(
            {
                "primary_contributor": primary["author"],
                "primary_contributor_login": primary.get("login"),
                "primary_contributor_lines": primary["lines"],
                "primary_contributor_percent": primary["percent"],
            }
        )
    return result


def attribute_file_data(
    data: dict,
    login_map: dict[str, str],
    aliases: dict[str, str],
    profiles: dict[str, str],
) -> dict:
    result = attribute_contributor_data(data.get("contributors"), login_map, aliases, profiles)
    latest = data.get("latest")
    if latest:
        latest_attr = attribute_commit(latest, login_map, aliases, profiles)
        result["latest_change_at"] = latest_attr["date"]
        result["latest_change_by"] = latest_attr["author"]
        result["latest_change_by_login"] = latest_attr["login"]
        if not result.get("primary_contributor") and latest_attr.get("author"):
            result["primary_contributor"] = latest_attr["author"]
            result["primary_contributor_login"] = latest_attr.get("login")
    return result
