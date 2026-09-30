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

The operator's own labels are evidence, applied to every non-restricted note
whether or not Jev is called: a frontmatter ``type:`` mapped by the taxonomy's
``type_doc_types`` sets the document type (confidence 1.0,
``doc_type_source="frontmatter"``) and Jev is not asked for it; a human tag
mapped by ``tag_topics`` always appears among the stored topics.

Project choices come from the taxonomy's ``project_sources``: ``work_subfolders``
(the immediate subfolders of the vault's ``Work/`` folder) is wired;
``tasks_projects`` and ``project_owners`` contribute no options yet.
"""
from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter

from api.services import chunker
from api.services.jev_client import JevClient
from api.services.vault_tag_store import TagRecord, VaultTagStore
from api.services.vault_taxonomy import Taxonomy, fold_label, load_taxonomy
from api.utils.date_parser import extract_note_date
from config.settings import settings

logger = logging.getLogger(__name__)

MAX_BODY_TOKENS = 8000
SECONDARY_TOPIC_MIN_P = 0.15
MAX_SECONDARY_TOPICS = 2
TOPIC_PARENT_ONLY_BELOW = 0.6
ACTIONABILITY_LEVELS = ["informational", "may need follow-up", "needs action"]

DEFAULT_RESTRICTED_TAGS = "therapy,private,finance,confidential"

_MODES = {"off", "shadow", "on"}


def _mode() -> str:
    mode = (settings.jev_vault_tagging or "off").strip().lower()
    if mode not in _MODES:
        logger.warning("Unrecognized LIFEOS_JEV_VAULT_TAGGING value; treating as off")
        return "off"
    return mode


def _segments(value: str, *, fold: bool) -> list[str] | None:
    """Normalized path segments of a path or configured entry, or None when it
    contains a `..` segment (never trusted). Whitespace and surrounding quotes
    are stripped; `.` segments and repeated or edge slashes disappear."""
    value = value.strip().strip("'\"").strip()
    if ".." in value.split("/"):
        return None
    norm = posixpath.normpath(value) if value else "."
    parts = [p for p in norm.split("/") if p and p != "."]
    return [p.casefold() for p in parts] if fold else parts


def _entries(raw: str, *, fold: bool) -> list[list[str]]:
    out = []
    for item in (raw or "").split(","):
        segs = _segments(item, fold=fold)
        if segs:
            out.append(segs)
    return out


def _starts_with(path: list[str], prefix: list[str]) -> bool:
    return len(path) >= len(prefix) and path[: len(prefix)] == prefix


def is_allowlisted(rel_path: str) -> bool:
    """True when the vault-relative path lies under an allowlisted folder prefix
    (whole path segments; `*` allows everything; a `..` segment never matches)."""
    path = _segments(rel_path, fold=False)
    if path is None:
        return False
    return any(e == ["*"] or _starts_with(path, e) for e in _entries(settings.jev_vault_tag_paths, fold=False))


def _restricted_entries() -> list[list[str]]:
    """Normalized entries of `LIFEOS_JEV_VAULT_RESTRICTED_PATHS`. A value that is
    empty after stripping means no path restriction; a non-empty value with no
    valid entry falls back to the default list."""
    raw = settings.jev_vault_restricted_paths or ""
    if not raw.strip():
        return []
    entries = _entries(raw, fold=True)
    if not entries:
        logger.warning("LIFEOS_JEV_VAULT_RESTRICTED_PATHS has no valid entry; using the default list")
        default = type(settings).model_fields["jev_vault_restricted_paths"].default
        entries = _entries(default, fold=True)
    return entries


def _path_restricted(rel_path: str) -> bool:
    """True when the path matches an entry of `LIFEOS_JEV_VAULT_RESTRICTED_PATHS`.

    An entry of one segment matches any folder of that name; a multi-segment
    entry matches as a vault-relative prefix, by whole segments.
    """
    path = _segments(rel_path, fold=True)
    if path is None:
        return True
    for entry in _restricted_entries():
        if len(entry) == 1:
            if entry[0] in path[:-1]:
                return True
        elif _starts_with(path, entry):
            return True
    return False


_INLINE_TAG = re.compile(r"(?:^|(?<=\s))#([\w/-]+)", re.M)
_FENCED_CODE = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)
_INLINE_CODE = re.compile(r"`[^`\n]*`")


def inline_tags(body: str) -> list[str]:
    """Obsidian inline tags (`#tag`, `#tag/sub`, `#1-1`, `#équipe`) outside code.

    A tag starts at line start or after whitespace and spans Unicode letters,
    digits, `_`, `-` and `/`. All-digit text after the hash, headings, URL anchors and
    code spans are excluded.
    """
    text = _INLINE_CODE.sub(" ", _FENCED_CODE.sub("", body))
    return [t for t in _INLINE_TAG.findall(text) if not t.isdigit()]


def _fold_tag(tag: str) -> str:
    """The one normal form for tags, applied to configured entries and note tags."""
    return tag.strip().lstrip("#").strip().casefold()


def _restricted_tags() -> frozenset[str]:
    """Normalized entries of `LIFEOS_JEV_VAULT_RESTRICTED_TAGS`. A value that is
    empty after stripping means no tag restriction; a non-empty value with no
    valid entry falls back to the default list."""
    raw = settings.jev_vault_restricted_tags or ""
    if not raw.strip():
        return frozenset()
    entries = _entries(raw, fold=True)
    if not entries:
        logger.warning("LIFEOS_JEV_VAULT_RESTRICTED_TAGS has no valid entry; using the default list")
        entries = _entries(DEFAULT_RESTRICTED_TAGS, fold=True)
    return frozenset(_fold_tag("/".join(e)) for e in entries)


def _tag_restricted(tag: str) -> bool:
    """A restricted tag or any of its children (`private/session`), case-insensitive."""
    restricted = _restricted_tags()
    parts = _fold_tag(tag).split("/")
    return any("/".join(parts[: i + 1]) in restricted for i in range(len(parts)))


def classify_sensitivity(rel_path: str, human_tags: list[str]) -> str:
    """``restricted`` on a restricted path or human tag, else ``private``. Code only."""
    if _path_restricted(rel_path):
        return "restricted"
    if any(_tag_restricted(t) for t in human_tags):
        return "restricted"
    return "private"


def _note_sensitivity(rel: str, resolved: str | None, tags: list[str], parsed: bool) -> str:
    """Sensitivity of a note by its path, symlink-resolved path, and human tags;
    an escaping symlink or unparsed frontmatter is ``restricted``."""
    if resolved is None or not parsed or classify_sensitivity(resolved, tags) == "restricted":
        return "restricted"
    return classify_sensitivity(rel, tags)


_RAW_TAGS_KEY = re.compile(r"^tags\s*:\s*(\S|\n\s*-)", re.M)


def parse_note(content: str) -> tuple[dict, str, bool]:
    """(frontmatter, body, parsed_ok). A `---` frontmatter block that has no
    closing `---` line (a `...` closer is not recognized), fails to parse, or
    whose raw text carries a `tags` key the parser did not return yields
    ok=False so the caller can treat the note as restricted."""
    content = content.removeprefix("\ufeff")
    lines = content.splitlines()
    if lines and lines[0].rstrip() == "---":
        closers = [i for i, line in enumerate(lines[1:], 1) if line.rstrip() == "---"]
        if not closers:
            return {}, content, False
        try:
            post = frontmatter.loads(content)
        except Exception:  # noqa: BLE001 - any parse failure
            return {}, content, False
        raw_block = "\n".join(lines[1 : closers[0]])
        if _RAW_TAGS_KEY.search(raw_block) and not post.metadata.get("tags"):
            return {}, content, False
        return dict(post.metadata), post.content, True
    return {}, content, True


def _cap_tokens(text: str, limit: int = MAX_BODY_TOKENS) -> str:
    if chunker.TOKENIZER is None:
        # a token is at least one UTF-8 byte, so limit bytes bound limit tokens
        return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
    if chunker.count_tokens(text) <= limit:
        return text
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
    fm, body, parsed = parse_note(content)
    from api.services.people import extract_people_from_text, people_from_tags

    rel = rel_path if rel_path is not None else path.name
    tags = chunker.normalize_tags(fm.get("tags")) + inline_tags(body)
    people = sorted(
        set(extract_people_from_text(body)) | set(chunker.normalize_tags(fm.get("people"))) | set(people_from_tags(tags))
    )
    return {
        "folder": str(Path(rel).parent) if Path(rel).parent != Path(".") else "",
        "note_date": extract_note_date(path, fm, body),
        "human_tags": tags,
        "people": people,
        "sensitivity": classify_sensitivity(rel, tags) if parsed else "restricted",
    }


def _label_evidence(tx: Taxonomy, fm: dict, tags: list[str]) -> tuple[str | None, dict[str, list[str]]]:
    """(doc_type from `type:`, {mapped topic: [human tags that map to it]})."""
    raw_type = fm.get("type")
    doc_type = tx.type_doc_types.get(fold_label(raw_type)) if isinstance(raw_type, str) else None
    topics: dict[str, list[str]] = {}
    for tag in tags:
        topic = tx.tag_topics.get(fold_label(tag))
        if topic and fold_label(tag) not in topics.setdefault(topic, []):
            topics[topic].append(fold_label(tag))
    return doc_type, topics


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
        """Vault-relative path as supplied (symlinks not followed)."""
        root = self.vault_root
        for base in (root, root.resolve()):
            try:
                return str(path.relative_to(base))
            except ValueError:
                pass
        try:
            return str(path.absolute().relative_to(root.resolve()))
        except ValueError:
            return path.name

    def _resolved_rel(self, path: Path) -> str | None:
        """Vault-relative path of the symlink-resolved target, None when outside the vault."""
        try:
            return str(path.resolve().relative_to(self.vault_root.resolve()))
        except ValueError:
            return None

    def jev_allowed(self, rel_path: str, resolved_rel: str | None, sensitivity: str) -> bool:
        """Both the path as supplied and the symlink-resolved target must be
        allowlisted, and the note must not be restricted."""
        return (
            _mode() in ("shadow", "on")
            and settings.jev_configured
            and resolved_rel is not None
            and is_allowlisted(rel_path)
            and is_allowlisted(resolved_rel)
            and sensitivity != "restricted"
        )

    def _questions(self, projects: list[str], ask_doc_type: bool = True) -> dict:
        tx = self.taxonomy
        questions = {
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
        if not ask_doc_type:
            del questions["doc_type"]
        return questions

    def _state(self, rel: str, fm: dict, body: str) -> dict:
        return {
            "path": rel,
            "title": Path(rel).stem,
            "frontmatter": _fm_subset(fm),
            "headings": _headings(body),
            "body": _cap_tokens(body),
        }

    def _apply_jev(
        self, record: TagRecord, answers: dict, projects: list[str], ask_doc_type: bool = True
    ) -> None:
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

        if ask_doc_type:
            record.doc_type, record.doc_type_conf = doc_type, doc_conf if doc_type else None
            record.doc_type_source = "jev"
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

    def _apply_evidence(
        self, record: TagRecord, doc_type: str | None, mapped: dict[str, list[str]]
    ) -> None:
        """Fold the operator's `type:` and mapped tags into the record."""
        detail = json.loads(record.topics_json or "{}")
        if doc_type:
            record.doc_type, record.doc_type_conf = doc_type, 1.0
            record.doc_type_source = "frontmatter"
            detail["doc_type"] = {doc_type: 1.0}
        if mapped:
            probs = detail.get("topic") or {}
            targets = sorted(mapped, key=lambda t: -probs.get(t, 0.0))  # stable: note order breaks ties
            parent_only = record.topic in self.taxonomy.domains
            promote = [t for t in targets if record.topic is None or (parent_only and t.startswith(record.topic + "/"))]
            if promote:
                record.topic, record.topic_conf = promote[0], 1.0
            elif record.topic in mapped:
                record.topic_conf = 1.0
            detail["secondary"] = [t for t in targets if t != record.topic] + [
                t for t in detail.get("secondary", []) if t not in mapped and t != record.topic
            ][:MAX_SECONDARY_TOPICS]
            detail["from_tags"] = {t: mapped[t] for t in sorted(mapped)}
        record.topics_json = json.dumps(detail, sort_keys=True)

    def would_send(self, path: str | Path) -> bool:
        """True when `tag_file` would send this note to Jev under current settings."""
        path = Path(path)
        try:
            rel = self._rel(path)
            fm, body, parsed = parse_note(path.read_text(encoding="utf-8", errors="replace"))
            tags = chunker.normalize_tags(fm.get("tags")) + inline_tags(body)
            resolved = self._resolved_rel(path)
            return self.jev_allowed(rel, resolved, _note_sensitivity(rel, resolved, tags, parsed))
        except Exception:  # noqa: BLE001
            return False

    def tag_file(self, path: str | Path) -> TagRecord:
        path = Path(path)
        rel = path.name
        previous = None
        try:
            rel = self._rel(path)
            previous = self.store.get(rel) if self.store else None
            content = path.read_text(encoding="utf-8", errors="replace")
            sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
            fm, body, parsed = parse_note(content)
            tags = chunker.normalize_tags(fm.get("tags")) + inline_tags(body)
            resolved = self._resolved_rel(path)
            sensitivity = _note_sensitivity(rel, resolved, tags, parsed)
            record = TagRecord(
                file_path=rel,
                content_sha256=sha,
                vocab_version=self.taxonomy.vocab_version,
                tagged_at=datetime.now(timezone.utc).isoformat(),
                sensitivity=sensitivity,
            )
            label_type, mapped = (
                (None, {}) if sensitivity == "restricted" else _label_evidence(self.taxonomy, fm, tags)
            )
            if self.jev_allowed(rel, resolved, sensitivity):
                projects = _project_options(self.vault_root, self.taxonomy)
                client = self._client or JevClient()
                questions = self._questions(projects, ask_doc_type=label_type is None)
                answers = client.ask(self._state(rel, fm, body), questions)
                record.model = getattr(client, "last_model", None) or getattr(client, "model", "") or ""
                self._apply_jev(record, answers, projects, ask_doc_type=label_type is None)
            self._apply_evidence(record, label_type, mapped)
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
