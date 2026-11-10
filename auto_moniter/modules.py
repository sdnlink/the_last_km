"""Monitor-owned module definitions and acceptance evidence."""

from __future__ import annotations

import copy
import json
import re
import fnmatch


def definitions(value: object) -> list[dict]:
    if not isinstance(value, list) or len(value) > 200:
        raise ValueError("modules must be an array of at most 200 module definitions")
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, dict) or not {"id", "title", "acceptance"} <= set(item) or set(item) - {"id", "title", "acceptance", "depends_on", "checks", "files"}:
            raise ValueError("each module needs exactly id, title, acceptance")
        ident = item["id"]
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", ident) or ident in seen:
            raise ValueError("module IDs must be unique stable identifiers (1-64 ASCII characters)")
        if not isinstance(item["title"], str) or not item["title"].strip() or len(item["title"]) > 500:
            raise ValueError("module title must be a nonempty string of at most 500 characters")
        criteria = item["acceptance"]
        if not isinstance(criteria, list) or not criteria or len(criteria) > 30 or any(not isinstance(text, str) or not text.strip() or len(text) > 2000 for text in criteria):
            raise ValueError("module acceptance must contain 1-30 nonempty strings, each at most 2000 characters")
        for field in ("depends_on", "checks", "files"):
            entries = item.get(field, [])
            if not isinstance(entries, list) or len(entries) > 30 or any(not isinstance(text, str) or not text.strip() or len(text) > 2000 for text in entries):
                raise ValueError(f"invalid module {field}")
        for pattern in item.get('files', []):
            from auto_moniter.quality import relative_path
            relative_path(pattern)
        if ident in item.get("depends_on", []):
            raise ValueError("module cannot depend on itself")
        seen.add(ident)
        result.append(copy.deepcopy(item))
    return result


class ModuleLedger:
    def __init__(self, plan: list[dict] | None = None) -> None:
        self.items: dict[str, dict] = {}
        self.merge(plan or [])

    def merge(self, proposal: object) -> None:
        proposed = definitions(proposal)
        for item in proposed:
            old = self.items.get(item["id"])
            if old and any(old.get(key, []) != item.get(key, []) for key in ("title", "acceptance", "depends_on", "checks", "files") if key in item):
                raise ValueError(f"module {item['id']} definition cannot change; preserve its original acceptance criteria")
        if len(self.items.keys() | {item['id'] for item in proposed}) > 200:
            raise ValueError("module ledger cannot exceed 200 entries")
        combined = {key: {field: item.get(field, []) for field in ("id", "title", "acceptance", "depends_on", "checks", "files")} for key, item in self.items.items()}
        combined.update({item["id"]: {**combined.get(item["id"], {}), **item} for item in proposed})
        if len(json.dumps(list(combined.values()), ensure_ascii=False).encode("utf-8")) > 128 * 1024:
            raise ValueError("module definitions cannot exceed 128 KiB in total")
        def visit(key, active, visited):
            if key in active:
                raise ValueError("module dependencies contain a cycle")
            if key in visited:
                return
            for dependency in combined[key].get("depends_on", []):
                if dependency not in combined:
                    raise ValueError(f"unknown module dependency {dependency}")
                visit(dependency, active | {key}, visited)
            visited.add(key)
        visited = set()
        for key in combined:
            visit(key, set(), visited)
        for item in proposed:
            self.items.setdefault(item["id"], {**item, "status": "pending", "evidence": [], "round": 0})

    def coverage(self, worker, reviewer=None) -> list[str]:
        verdicts = [worker] if reviewer is None else [worker, reviewer]
        errors = []
        if not self.items:
            errors.append("Declare the full original requirement as modules with stable IDs and acceptance criteria before claiming completion.")
        for verdict in verdicts:
            if len(set(verdict.module_ids)) != len(verdict.module_ids) or set(verdict.module_ids) - self.items.keys():
                errors.append("Completion names duplicate or unknown module IDs; use the monitor module ledger.")
            if verdict.scope == "module" and not verdict.module_ids:
                errors.append("Module completion must name the completed module_ids.")
        covered = set(worker.module_ids)
        if reviewer is not None:
            covered &= set(reviewer.module_ids)
        for key in covered & self.items.keys():
            missing_deps = [dep for dep in self.items[key].get("depends_on", []) if self.items[dep]["status"] != "accepted" and dep not in covered]
            if missing_deps:
                errors.append(f"Module {key} requires accepted dependencies: {', '.join(missing_deps)}")
        if all(verdict.scope == "requirement" for verdict in verdicts):
            missing = {key for key, item in self.items.items() if item["status"] != "accepted"} - covered
            if missing:
                errors.append("Original requirement still has unaccepted modules: " + ", ".join(sorted(missing)))
        elif not any(self.items[key]["status"] != "accepted" for key in covered if key in self.items):
            errors.append("No new module was jointly accepted; implement the next pending module or verify the whole requirement.")
        return errors

    def accept(self, worker, reviewer, index: int, tree: str = "") -> None:
        covered = set(worker.module_ids)
        if reviewer is not None:
            covered &= set(reviewer.module_ids)
        for key in covered:
            if key in self.items and self.items[key]["status"] != "accepted":
                evidence = [text.encode("utf-8")[:1024].decode("utf-8", errors="ignore") for text in (reviewer or worker).evidence[:4]]
                self.items[key].update(status="accepted", evidence=evidence, round=index, tree=tree, invalidated_reason="")

    def affected(self, changed_files: list[str] | None = None) -> set[str]:
        """Missing scopes and shared files conservatively require revalidation."""
        affected = set(self.items)
        if changed_files is not None:
            affected = {key for key, m in self.items.items() if not self.impact_files(key) or any(
                fnmatch.fnmatchcase(path, pattern) for path in changed_files for pattern in self.impact_files(key))}
            # Undeclared files may be shared configuration; fail conservatively.
            if any(not any(fnmatch.fnmatchcase(path, pattern) for key in self.items for pattern in self.impact_files(key))
                   for path in changed_files):
                affected = set(self.items)
            while True:
                more = {key for key, m in self.items.items() if set(m.get('depends_on', [])) & affected}
                if more <= affected:
                    break
                affected |= more
        return affected

    def impact_files(self, key):
        item = self.items[key]
        return item.get('files') or item.get('review_files', [])

    def review_scope(self, key, paths):
        if key not in self.items:
            raise ValueError('Review scope names an unknown module')
        definitions([{'id': key, 'title': key, 'acceptance': ['scope'], 'files': paths}])
        if paths and not self.items[key].get('files'):
            combined = list(dict.fromkeys([*self.items[key].get('review_files', []), *paths]))
            definitions([{'id': key, 'title': key, 'acceptance': ['scope'], 'files': combined}])
            self.items[key]['review_files'] = combined

    def invalidate(self, tree: str, changed_files: list[str] | None = None) -> list[str]:
        affected = self.affected(changed_files)
        changed = []
        for key, item in self.items.items():
            if item["status"] == "accepted" and item.get("tree") != tree:
                if key not in affected:
                    item['tree'] = tree
                    continue
                item["status"] = "pending"
                item["invalidated_reason"] = "Revalidation required; historical acceptance retained, no reproduced regression yet"
                changed.append(key)
        return changed

    def checks_for(self, module_ids: set[str]) -> list[str]:
        ordered = []
        visited = set()
        def visit(key):
            if key in visited or key not in self.items:
                return
            visited.add(key)
            for dependency in self.items[key].get("depends_on", []):
                visit(dependency)
            ordered.extend(self.items[key].get("checks", []))
        for key in self.items:
            if key in module_ids:
                visit(key)
        return list(dict.fromkeys(ordered))

    def export(self) -> list[dict]:
        return copy.deepcopy(list(self.items.values()))

    @classmethod
    def restore(cls, value: object) -> ModuleLedger:
        if not isinstance(value, list):
            raise ValueError("invalid saved module ledger")
        if len(value) > 200:
            raise ValueError("too many saved modules")
        plan = [{key: item.get(key, []) for key in ("id", "title", "acceptance", "depends_on", "checks", "files")} for item in value if isinstance(item, dict)]
        ledger = cls(plan)
        seen = set()
        for item in value:
            if not isinstance(item, dict) or item.get("status") not in {"pending", "accepted"}:
                raise ValueError("invalid saved module status")
            definitions([{key: item.get(key, []) for key in ("id", "title", "acceptance", "depends_on", "checks")}])
            if not isinstance(item.get("evidence"), list) or any(not isinstance(text, str) for text in item["evidence"]):
                raise ValueError("invalid saved module evidence")
            if not isinstance(item.get("round"), int) or item["round"] < 0 or item.get("id") in seen:
                raise ValueError("invalid saved module acceptance round or duplicate ID")
            seen.add(item["id"])
            ledger.items[item["id"]].update(status=item["status"], evidence=item["evidence"], round=item["round"], tree=item.get("tree", ""), invalidated_reason=item.get("invalidated_reason", ""))
            ledger.review_scope(item['id'], item.get('review_files', []))
        return ledger
