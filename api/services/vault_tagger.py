"""Per-note vault tagging.

``VaultTagger.tag_file`` derives a ``TagRecord`` for one note. Code-only facets
(content hash, vocabulary version, sensitivity class, and via
``deterministic_facets`` the folder, date, human tags and people) are always
available. The closed-set facets (document type, domain, topic, project,
actionability, has-decision) come from one Jev call per note, and only when
every gate holds:

- ``LIFEOS_JEV_VAULT_TAGGING`` is ``shadow`` or ``on``,
- a TypeSafe API key is configured,
- the note lies under an entry of ``LIFEOS_JEV_VAULT_TAG_PATHS`` (``*`` = all),
- the note's on-box sensitivity class is not ``restricted``.

Sensitivity is decided by code alone (path rules and human tags) because
delegating it would mean sending the note to be classified. ``shadow`` and
``on`` tag identically; the distinction is for consumers of the store.

The state sent to Jev (path, title, a frontmatter subset, headings, body capped
at 8,000 tokens) is never logged. Any failure returns the previous record if one
exists, else a code-only record; ``tag_file`` never raises.

Project choices come from the taxonomy's ``project_sources``: ``work_subfolders``
(the immediate subfolders of the vault's ``Work/`` folder) is wired;
``tasks_projects`` and ``project_owners`` contribute no options yet.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api.services import chunker
from api.services.jev_client import JevClient
from api.services.vault_tag_store import TagRecord, VaultTagStore
from api.services.vault_taxonomy import Taxonomy, load_taxonomy
from api.utils.date_parser import extract_note_date
from config.settings import settings

logger = logging.getLogger(__name__)

MAX_BODY_TOKENS = 8000
SECONDARY_TOPIC_MIN_P = 0.15
MAX_SECONDARY_TOPICS = 2
TOPIC_PARENT_ONLY_BELOW = 0.6
ACTIONABILITY_LEVELS = ["informational", "may need follow-up", "needs action"]

RESTRICTED_FOLDERS = frozenset({"lifelogs", "omi", "therapy", "relationship", "finance"})
RESTRICTED_TAGS = frozenset({"therapy", "private", "finance", "confidential"})

_MODES = {"off", "shadow", "on"}


def _mode() -> str:
    mode = (settings.jev_vault_tagging or "off").strip().lower()
    if mode not in _MODES:
        logger.warning("Unrecognized LIFEOS_JEV_VAULT_TAGGING value; treating as off")
        return "off"
    return mode


def _allowed_prefixes() -> list[str]:
    return [p.strip().strip("/") for p in (settings.jev_vault_tag_paths or "").split(",") if p.strip()]


def is_allowlisted(rel_path: str) -> bool:
    """True when the vault-relative path lies under an allowlisted folder prefix."""
    rel = rel_path.strip("/")
    for prefix in _allowed_prefixes():
        if prefix == "*" or rel == prefix or rel.startswith(prefix + "/"):
            return True
    return False


def classify_sensitivity(rel_path: str, human_tags: list[str]) -> str:
    """``restricted`` on a restricted folder name or human tag, else ``private``. Code only."""
    folders = {part.lower() for part in Path(rel_path).parts[:-1]}
    if folders & RESTRICTED_FOLDERS:
        return "restricted"
    if {t.lower() for t in human_tags} & RESTRICTED_TAGS:
        return "restricted"
    return "private"


def _cap_tokens(text: str, limit: int = MAX_BODY_TOKENS) -> str:
    if chunker.count_tokens(text) <= limit:
        return text
    if chunker.TOKENIZER is None:
        return text[: limit * 4]
    return chunker.TOKENIZER.decode(chunker.TOKENIZER.encode(text)[:limit])


def _headings(body: str, limit: int = 40) -> list[str]:
    found = [line.strip() for line in body.splitlines() if line.lstrip().startswith("#")]
    return found[:limit]


def _fm_subset(fm: dict) -> dict:
    out: dict[str, Any] = {}
    for key in ("date", "created", "type"):
        if fm.get(key) is not None:
            out[key] = str(fm[key])
    tags = chunker.normalize_tags(fm.get("tags"))
    if tags:
        out["tags"] = tags
    return out


def deterministic_facets(path: Path, content: str | None = None, rel_path: str | None = None) -> dict:
    """Code-derived facets: folder, note_date, human_tags, people, sensitivity."""
    if content is None:
        content = path.read_text(encoding="utf-8", errors="replace")
    fm, body = chunker.extract_frontmatter(content)
    from api.services.people import extract_people_from_text

    rel = rel_path if rel_path is not None else path.name
    tags = chunker.normalize_tags(fm.get("tags"))
    people = sorted(set(extract_people_from_text(body)) | set(chunker.normalize_tags(fm.get("people"))))
    return {
        "folder": str(Path(rel).parent) if Path(rel).parent != Path(".") else "",
        "note_date": extract_note_date(path, fm, body),
        "human_tags": tags,
        "people": people,
        "sensitivity": classify_sensitivity(rel, tags),
    }


def _project_options(vault_root: Path, taxonomy: Taxonomy) -> list[str]:
    if "work_subfolders" not in taxonomy.project_sources:
        return []
    work = vault_root / "Work"
    if not work.is_dir():
        return []
    return sorted(p.name for p in work.iterdir() if p.is_dir() and not p.name.startswith("."))


def _distribution(answer: Any, options: set[str]) -> tuple[str | None, float, dict[str, float]]:
    """(argmax choice, its confidence, {option: p}) from a Jev choice answer."""
    if not isinstance(answer, dict):
        return None, 0.0, {}
    raw = answer.get("probabilities")
    probs = {
        k: float(v) for k, v in (raw.items() if isinstance(raw, dict) else [])
        if k in options and isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    choice = max(probs, key=probs.get) if probs else answer.get("choice")
    if choice not in options:
        return None, 0.0, probs
    conf = probs.get(choice)
    if conf is None:
        c = answer.get("confidence")
        conf = float(c) if isinstance(c, (int, float)) and not isinstance(c, bool) else 0.0
    return choice, conf, probs


def _number(answer: Any, key: str) -> float | None:
    v = answer.get(key) if isinstance(answer, dict) else None
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class VaultTagger:
    def __init__(
        self,
        store: VaultTagStore | None = None,
        client: JevClient | None = None,
        taxonomy: Taxonomy | None = None,
        vault_root: Path | None = None,
    ):
        self.store = store
        self._client = client
        self._taxonomy = taxonomy
        self.vault_root = Path(vault_root) if vault_root else Path(settings.vault_path)

    @property
    def taxonomy(self) -> Taxonomy:
        if self._taxonomy is None:
            self._taxonomy = load_taxonomy()
        return self._taxonomy

    def _rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.vault_root.resolve()))
        except ValueError:
            return path.name

    def jev_allowed(self, rel_path: str, sensitivity: str) -> bool:
        return (
            _mode() in ("shadow", "on")
            and settings.jev_configured
            and is_allowlisted(rel_path)
            and sensitivity != "restricted"
        )

    def _questions(self, projects: list[str]) -> dict:
        tx = self.taxonomy
        return {
            "doc_type": {
                "type": "choice",
                "instructions": "Which kind of document is this note?",
                "criteria": {d: d.replace("_", " ") for d in tx.doc_types},
            },
            "domain": {
                "type": "choice",
                "instructions": "Which area of life does this note mainly belong to?",
                "criteria": {d: d.replace("_", " ") for d in tx.domains},
            },
            "topic": {
                "type": "choice",
                "instructions": "Which single topic best describes what this note is about?",
                "criteria": {name: desc or name for name, desc in tx.topics.items()},
            },
            "project": {
                "type": "choice",
                "instructions": "Which project, if any, is this note part of?",
                "criteria": {**{p: p for p in projects}, "none": "Not part of any listed project"},
            },
            "actionability": {
                "type": "score",
                "instructions": "How much action does this note call for?",
                "criteria": ACTIONABILITY_LEVELS,
            },
            "has_decision": {
                "type": "noul",
                "instructions": "This note records a decision that was made.",
            },
        }

    def _state(self, rel: str, fm: dict, body: str) -> dict:
        return {
            "path": rel,
            "title": Path(rel).stem,
            "frontmatter": _fm_subset(fm),
            "headings": _headings(body),
            "body": _cap_tokens(body),
        }

    def _apply_jev(self, record: TagRecord, answers: dict, projects: list[str]) -> None:
        tx = self.taxonomy
        doc_type, doc_conf, doc_p = _distribution(answers.get("doc_type"), set(tx.doc_types))
        domain, dom_conf, dom_p = _distribution(answers.get("domain"), set(tx.domains))
        topic, top_conf, top_p = _distribution(answers.get("topic"), set(tx.topics))
        project, proj_conf, proj_p = _distribution(answers.get("project"), set(projects) | {"none"})

        secondary = sorted(
            (t for t, p in top_p.items() if t != topic and p >= SECONDARY_TOPIC_MIN_P),
            key=lambda t: -top_p[t],
        )[:MAX_SECONDARY_TOPICS]
        if topic and top_conf < TOPIC_PARENT_ONLY_BELOW:
            topic = topic.split("/")[0]

        record.doc_type, record.doc_type_conf = doc_type, doc_conf if doc_type else None
        record.domain, record.domain_conf = domain, dom_conf if domain else None
        record.topic, record.topic_conf = topic, top_conf if topic else None
        record.project = None if project in (None, "none") else project
        record.project_conf = proj_conf if project else None
        record.actionability = _number(answers.get("actionability"), "score")
        record.has_decision = _number(answers.get("has_decision"), "noul")
        record.topics_json = json.dumps(
            {"doc_type": doc_p, "domain": dom_p, "topic": top_p, "project": proj_p, "secondary": secondary},
            sort_keys=True,
        )
        record.backend = "jev"

    def tag_file(self, path: str | Path) -> TagRecord:
        path = Path(path)
        rel = self._rel(path)
        previous = None
        try:
            previous = self.store.get(rel) if self.store else None
            content = path.read_text(encoding="utf-8", errors="replace")
            sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
            fm, body = chunker.extract_frontmatter(content)
            sensitivity = classify_sensitivity(rel, chunker.normalize_tags(fm.get("tags")))
            record = TagRecord(
                file_path=rel,
                content_sha256=sha,
                vocab_version=self.taxonomy.vocab_version,
                tagged_at=datetime.now(timezone.utc).isoformat(),
                sensitivity=sensitivity,
            )
            if not self.jev_allowed(rel, sensitivity):
                return record
            projects = _project_options(self.vault_root, self.taxonomy)
            client = self._client or JevClient()
            answers = client.ask(self._state(rel, fm, body), self._questions(projects))
            record.model = getattr(client, "last_model", None) or getattr(client, "model", "") or ""
            self._apply_jev(record, answers, projects)
            return record
        except Exception as exc:  # noqa: BLE001 - tagging must never raise
            logger.warning("Vault tagging fell back to code-only: %s", type(exc).__name__)
            if previous is not None:
                return previous
            return TagRecord(
                file_path=rel,
                content_sha256="",
                vocab_version=self._safe_vocab(),
                tagged_at=datetime.now(timezone.utc).isoformat(),
                sensitivity=classify_sensitivity(rel, []),
            )

    def _safe_vocab(self) -> str:
        try:
            return self.taxonomy.vocab_version
        except Exception:  # noqa: BLE001
            return ""
