"""Bounded, monitor-owned audit issues. Only independent reviews close issues."""

from __future__ import annotations

import copy
import json
import re

from auto_moniter.redaction import redact


MAX_OPEN = 128
KEEP_CLOSED = 20
MAX_BYTES = 512 * 1024


class AuditCapacityError(ValueError):
    pass


def _text(value: object, limit: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
        raise ValueError(f"Audit text must be nonempty and at most {limit} UTF-8 bytes")
    return value.strip()


def resolutions(value: object) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_OPEN:
        raise ValueError("issue_resolutions must be an array of at most 128 entries")
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "summary", "evidence"}:
            raise ValueError("Each issue resolution needs id, summary and evidence")
        ident = item["id"]
        if not isinstance(ident, str) or not re.fullmatch(r"AUD-[1-9][0-9]{0,11}", ident) or ident in seen:
            raise ValueError("Issue resolution IDs must be unique monitor-assigned AUD numbers")
        _text(item["summary"])
        evidence = item["evidence"]
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 4:
            raise ValueError("Issue resolution needs 1-4 evidence strings")
        for text in evidence:
            _text(text, 1024)
        seen.add(ident)
        result.append(copy.deepcopy(item))
    return result


class AuditLedger:
    def __init__(self):
        self.items: list[dict] = []
        self.next_id = 1
        self.pruned_closed = 0

    def export(self) -> dict:
        return {"next_id": self.next_id, "pruned_closed": self.pruned_closed, "items": copy.deepcopy(self.items)}

    def _commit(self, items: list[dict], next_id: int) -> None:
        if sum(item["status"] == "open" for item in items) > MAX_OPEN:
            raise AuditCapacityError("Open audit issues exceed 128; resolve existing issues before continuing")
        closed = sorted((item for item in items if item["status"] == "resolved"),
                        key=lambda item: (item["resolved_round"], int(item["id"][4:])))
        keep_closed = min(KEEP_CLOSED, MAX_OPEN - sum(item["status"] == "open" for item in items))
        dropped = {item["id"] for item in closed[:max(0, len(closed) - keep_closed)]}
        items = [item for item in items if item["id"] not in dropped]
        value = {"next_id": next_id, "pruned_closed": self.pruned_closed + len(dropped), "items": items}
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
            raise AuditCapacityError("Audit issue ledger exceeds 512 KiB; no open issue was discarded")
        self.items, self.next_id, self.pruned_closed = items, next_id, value["pruned_closed"]

    def record(self, findings: list[str], index: int, module_ids: list[str] | None = None) -> list[str]:
        items = copy.deepcopy(self.items)
        next_id = self.next_id
        added = []
        modules = sorted(set(module_ids or []))
        for finding in findings:
            text, old = self._match(finding, modules, items)
            if old:
                old.update(status="open", last_seen_round=index, resolution=None, resolved_round=0, tree="")
                added.append(old["id"])
                continue
            ident = f"AUD-{next_id}"
            next_id += 1
            items.append({"id": ident, "summary": text, "module_ids": modules, "status": "open",
                          "first_seen_round": index, "last_seen_round": index, "resolved_round": 0,
                          "resolution": None, "tree": ""})
            added.append(ident)
        self._commit(items, next_id)
        return added

    @staticmethod
    def _match(finding: str, modules: list[str], items: list[dict]) -> tuple[str, dict | None]:
        if isinstance(finding, str) and len(finding.encode("utf-8")) > 2000:
            raise AuditCapacityError("Audit finding exceeds 2000 UTF-8 bytes; simplify the finding before continuing")
        text = redact(_text(finding))
        reference = re.match(r"^(AUD-[1-9][0-9]{0,11})(?=$|[:：\s])", text)
        if reference:
            old = next((item for item in items if item["id"] == reference[1]), None)
            if old is None:
                raise ValueError(f"Unknown audit issue {reference[1]}; use the monitor ledger")
            return text, old
        key = " ".join(text.split()).casefold()
        return text, next((item for item in items if " ".join(item["summary"].split()).casefold() == key
                           and item["module_ids"] == modules), None)

    def update(self, findings: list[str], proposals: object, index: int, tree: str,
               module_ids: list[str], check_evidence) -> tuple[list[str], list[str]]:
        """Apply valid closures and new findings together within the capacity budget."""
        module_ids = sorted(set(module_ids))
        try:
            proposals = resolutions(proposals)
            mentioned = set()
            for finding in findings:
                _, old = self._match(finding, module_ids, self.items)
                if old:
                    mentioned.add(old["id"])
            if mentioned.intersection(item["id"] for item in proposals):
                raise ValueError("An issue cannot be closed while the same review reports it as an open finding")
            staged = copy.deepcopy(self)
            closed = staged.resolve(proposals, index, tree, check_evidence)
            reported = staged.record(findings, index, module_ids)
        except AuditCapacityError:
            raise
        except ValueError:
            # An invalid closure must not erase otherwise valid new findings.
            self.record(findings, index, module_ids)
            raise
        self.items, self.next_id, self.pruned_closed = staged.items, staged.next_id, staged.pruned_closed
        return reported, closed

    def resolve(self, proposals: object, index: int, tree: str, check_evidence) -> list[str]:
        proposals = resolutions(proposals)
        items = copy.deepcopy(self.items)
        for proposal in proposals:
            item = next((item for item in items if item["id"] == proposal["id"]), None)
            if item is None:
                raise ValueError(f"Unknown audit issue {proposal['id']}; use the monitor ledger")
            problems = check_evidence(proposal["evidence"])
            if problems:
                raise ValueError(f"Issue {item['id']} closure evidence rejected: " + "; ".join(problems))
            item.update(status="resolved", resolved_round=index, tree=tree,
                        resolution={"summary": redact(proposal["summary"]),
                                    "evidence": [redact(text) for text in proposal["evidence"]]})
        self._commit(items, self.next_id)
        return [item["id"] for item in proposals]

    def invalidate(self, tree: str, affected_modules: set[str] | None = None,
                   changed_files: list[str] | None = None) -> list[str]:
        reopened = []
        for item in self.items:
            if item["status"] == "resolved" and item["tree"] != tree:
                from auto_moniter.quality import cited
                evidence_changed = changed_files is not None and any(
                    cited([name], item['resolution']['evidence']) for name in changed_files)
                if (affected_modules is not None and item['module_ids']
                        and not affected_modules.intersection(item['module_ids']) and not evidence_changed):
                    item['tree'] = tree
                    continue
                item.update(status="open", resolution=None, resolved_round=0, tree="")
                reopened.append(item["id"])
        return reopened

    def pending(self, module_ids: set[str] | None = None) -> list[dict]:
        return copy.deepcopy([item for item in self.items if item["status"] == "open"
                              and (module_ids is None or not item["module_ids"] or module_ids.intersection(item["module_ids"]))])

    def prompt(self) -> list[dict]:
        return [{key: item[key] for key in ("id", "summary", "module_ids")} for item in self.pending()]

    @classmethod
    def restore(cls, value: object) -> AuditLedger:
        if not isinstance(value, dict) or set(value) != {"next_id", "pruned_closed", "items"}:
            raise ValueError("Invalid saved audit ledger")
        result = cls()
        for key in ("next_id", "pruned_closed"):
            if type(value[key]) is not int or value[key] < (1 if key == "next_id" else 0):
                raise ValueError("Invalid saved audit counters")
        entries = value["items"]
        if not isinstance(entries, list) or len(entries) > MAX_OPEN:
            raise ValueError("Invalid saved audit items")
        if value["next_id"] > 10**12 or value["pruned_closed"] + len(entries) != value["next_id"] - 1:
            raise ValueError("Saved audit counters do not match retained history")
        seen = set()
        for item in entries:
            if not isinstance(item, dict) or set(item) != {"id", "summary", "module_ids", "status", "first_seen_round", "last_seen_round", "resolved_round", "resolution", "tree"}:
                raise ValueError("Invalid saved audit issue")
            ident = item["id"]
            if not isinstance(ident, str) or not re.fullmatch(r"AUD-[1-9][0-9]{0,11}", ident) or ident in seen or int(ident[4:]) >= value["next_id"]:
                raise ValueError("Invalid saved audit ID")
            _text(item["summary"])
            if not isinstance(item["module_ids"], list) or len(item["module_ids"]) > 200 or any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", key) for key in item["module_ids"]):
                raise ValueError("Invalid saved audit module IDs")
            if len(set(item["module_ids"])) != len(item["module_ids"]):
                raise ValueError("Duplicate saved audit module IDs")
            if any(type(item[key]) is not int or item[key] < 0 for key in ("first_seen_round", "last_seen_round", "resolved_round")):
                raise ValueError("Invalid saved audit rounds")
            if item["last_seen_round"] < item["first_seen_round"] or not isinstance(item["tree"], str):
                raise ValueError("Invalid saved audit history")
            if item["status"] == "resolved":
                if not re.fullmatch(r"[0-9a-f]{64}", item["tree"]) or item["resolved_round"] < item["last_seen_round"]:
                    raise ValueError("Invalid saved audit closure")
                if not isinstance(item["resolution"], dict) or set(item["resolution"]) != {"summary", "evidence"}:
                    raise ValueError("Invalid saved audit closure")
                resolutions([{"id": ident, **item["resolution"]}])
            elif item["status"] != "open" or item["resolution"] is not None or item["tree"] or item["resolved_round"]:
                raise ValueError("Invalid saved open audit issue")
            seen.add(ident)
        result._commit(copy.deepcopy(entries), value["next_id"])
        if len(result.items) != len(entries):
            raise ValueError("Saved audit retention exceeds limits")
        result.pruned_closed = value["pruned_closed"]
        return result
