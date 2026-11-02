#!/usr/bin/env python3
"""
Re-resolve note mentions linked to one-word people, and retire the empty rows.

A bare row is a visible, unmerged person with a one-word name and no email or
phone. This repair, in order:

* removes interactions and source entities that come from bulk-content notes
  (LIFEOS_VAULT_NO_PEOPLE_PATHS, frontmatter people: false or generated:) or from
  notes under _-prefixed folders, for anyone except the protected rows below
* re-resolves each mention on a bare row, per note, with the mention resolver:
  on a clear winner the source entity moves to that person, along with the
  note's interaction if this row held it; otherwise the link is removed and the
  interaction with it
* hides each bare row left with nothing. Hiding is reversible, and consistency
  verification only removes a hidden row's interactions and inferred
  relationships, so nothing of value goes with it

A bare row is protected, and left exactly as it is, when anything on it came
from a human or from outside notes: link overrides, a confirmed or rejected
link, person facts, photos, any interaction or source entity outside the vault,
strength or circle overrides, tags, notes, a birthday, profile fields, family
category, a relationship that discovery doesn't infer, a review marked kept.
Those rows go in the report, counted separately.

Set LIFEOS_VAULT_NO_PEOPLE_PATHS before running, so bulk-content folders count.

Dry run by default, using read-only connections and writing only the report.
--execute backs up both databases first, then applies exactly what a dry run
would report. Running again finds nothing to do. Output is aggregate counts;
note paths, names, and sampled decisions go only to the private report file.

Usage:
    python3 scripts/repair_bare_person_mentions.py \\
        --crm-db data/crm.db --interactions-db data/interactions.db \\
        --vault-tags-db data/vault_tags.db --vault "$LIFEOS_VAULT_PATH"
    python3 scripts/repair_bare_person_mentions.py ... --execute

Afterwards rebuild derived data: scripts/sync_strengths.py --execute, then
scripts/sync_crm_to_vectorstore.py --execute.
"""
import argparse
import json
import logging
import os
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.services.chunker import extract_frontmatter  # noqa: E402
from api.services.entity_resolver import is_bare_person  # noqa: E402
from api.services.mention_resolver import (  # noqa: E402
    MENTION_MATCH_TYPE,
    REASON_LINKED,
    MentionDecision,
    MentionResolver,
    build_note_context,
    is_ambiguous_mention,
    load_link_overrides,
    load_tag_domain,
)
from api.services.people import people_in_note  # noqa: E402
from api.services.person_entity import PersonEntity, PersonEntityStore  # noqa: E402
from api.services.relationship import TYPE_COWORKER, TYPE_INFERRED  # noqa: E402
from api.services.vault_listing import (  # noqa: E402
    in_underscore_dir,
    no_people_reason,
    vault_relative_path,
)
from api.utils.date_parser import extract_note_date  # noqa: E402
from config.employment_history import EmploymentHistory, get_employment_history  # noqa: E402
from config.relationship_weights import (  # noqa: E402
    CIRCLE_OVERRIDES_BY_ID,
    STRENGTH_OVERRIDES_BY_ID,
)
from config.settings import settings  # noqa: E402

logger = logging.getLogger(__name__)

HIDDEN_REASON = "one-word row from note mentions with nothing left after mention repair"
MENTION_SOURCES = ("vault", "granola")
SAMPLE_SIZE = 40
SAMPLE_SEED = 20261102
DEFAULT_REPORT_DIR = Path.home() / ".lifeos-private" / "mention-repair"
_CHUNK = 500


def _connect_read_only(path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, timeout=60.0)


def _rows(conn: sqlite3.Connection, sql: str) -> list[tuple]:
    """A query's rows, or none on an install that hasn't created the table."""
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []


def _note_path_and_name(source_id: str) -> tuple[str, str]:
    """A mention's source_id is ``<note path>:<name>``."""
    position = source_id.rfind(".md:")
    if position < 0:
        return source_id, ""
    return source_id[:position + 3], source_id[position + 4:]


@dataclass
class Link:
    """A source entity from a note."""

    id: str
    note_path: str
    name: str
    person_id: Optional[str]
    status: str


@dataclass
class NoteInteraction:
    id: str
    note_path: str
    person_id: str
    source_type: str


@dataclass
class NoteRows:
    """Everything linked to one note, across any location it was stored under."""

    links: list[Link] = field(default_factory=list)
    interactions: list[NoteInteraction] = field(default_factory=list)


@dataclass
class Move:
    from_id: str
    to_id: str
    mention: str
    rel_path: str
    note_path: str
    source_type: str
    decision: MentionDecision
    link_ids: list[str] = field(default_factory=list)
    interaction_ids: list[str] = field(default_factory=list)


@dataclass
class Plan:
    counts: Counter = field(default_factory=Counter)
    moves: list[Move] = field(default_factory=list)
    unlink_ids: list[str] = field(default_factory=list)
    delete_link_ids: list[str] = field(default_factory=list)
    delete_interaction_ids: list[str] = field(default_factory=list)
    hide_ids: list[str] = field(default_factory=list)
    affected_ids: set[str] = field(default_factory=set)
    protected: dict[str, list[str]] = field(default_factory=dict)
    unlink_samples: list[dict] = field(default_factory=list)
    no_people_notes: Counter = field(default_factory=Counter)


# ---------------------------------------------------------------------- load


def load_merged_ids(path: Path) -> dict[str, str]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def load_people(crm_db: Path, merged_ids: dict) -> list[PersonEntity]:
    """Visible, unmerged people, read without opening the store for writing."""
    conn = _connect_read_only(crm_db)
    conn.row_factory = sqlite3.Row
    try:
        return [
            PersonEntityStore._row_to_entity(None, row)
            for row in conn.execute("SELECT * FROM person_entities WHERE hidden = 0")
            if row["id"] not in merged_ids
        ]
    finally:
        conn.close()


def _load_evidence(crm_db: Path, interactions_db: Path, bare_ids: set[str]) -> dict[str, list[str]]:
    """Why each bare row must not change, from every table that could say so."""
    reasons: dict[str, list[str]] = defaultdict(list)

    def add(person_id: Optional[str], reason: str) -> None:
        if person_id in bare_ids and reason not in reasons[person_id]:
            reasons[person_id].append(reason)

    conn = _connect_read_only(crm_db)
    try:
        for preferred, rejected in _rows(
            conn, "SELECT preferred_person_id, rejected_person_id FROM link_overrides"
        ):
            add(preferred, "link_override")
            add(rejected, "link_override")
        for (person_id,) in _rows(conn, "SELECT person_id FROM person_facts"):
            add(person_id, "person_facts")
        for (person_id,) in _rows(conn, "SELECT person_id FROM relationship_insights"):
            add(person_id, "relationship_insight")
        for person_id, source, status in _rows(
            conn,
            "SELECT DISTINCT canonical_person_id, source_type, link_status FROM source_entities "
            "WHERE canonical_person_id IS NOT NULL",
        ):
            if source not in MENTION_SOURCES:
                add(person_id, f"source_entity:{source}")
            if status != "auto":
                add(person_id, f"link_status:{status}")
        for person_a, person_b, kind in _rows(
            conn, "SELECT person_a_id, person_b_id, relationship_type FROM relationships"
        ):
            if kind not in (TYPE_INFERRED, TYPE_COWORKER):
                add(person_a, f"relationship:{kind}")
                add(person_b, f"relationship:{kind}")
        for person_a, person_b in _rows(
            conn, "SELECT person_a_id, person_b_id FROM entity_review_queue WHERE status = 'kept'"
        ):
            add(person_a, "review_kept")
            add(person_b, "review_kept")
    finally:
        conn.close()

    conn = _connect_read_only(interactions_db)
    try:
        for person_id, source in conn.execute(
            "SELECT DISTINCT person_id, source_type FROM interactions"
        ):
            if source not in MENTION_SOURCES:
                add(person_id, f"interaction:{source}")
    finally:
        conn.close()
    return reasons


def protection_reasons(person: PersonEntity, evidence: dict[str, list[str]]) -> list[str]:
    """Every reason a bare row must be left for a human."""
    reasons = list(evidence.get(person.id, []))
    if person.id in STRENGTH_OVERRIDES_BY_ID:
        reasons.append("strength_override")
    if person.id in CIRCLE_OVERRIDES_BY_ID:
        reasons.append("circle_override")
    if person.category == "family":
        reasons.append("family_category")
    if person.photo_count:
        reasons.append("photos")
    for label, value in (
        ("tags", person.tags), ("notes", person.notes), ("birthday", person.birthday),
        ("linkedin_url", person.linkedin_url), ("company", person.company),
        ("position", person.position), ("aliases", person.aliases),
    ):
        if value:
            reasons.append(label)
    if set(person.sources) - {*MENTION_SOURCES, "calendar", "gmail", "slack", "imessage", "whatsapp", "phone", "contacts"}:
        reasons.append("manual_source")
    return reasons


def _load_note_rows(crm_db: Path, interactions_db: Path) -> dict[str, NoteRows]:
    """Every mention row, keyed by the note path it was stored under."""
    notes: dict[str, NoteRows] = defaultdict(NoteRows)
    conn = _connect_read_only(crm_db)
    try:
        for link_id, source_id, observed, person_id, status in conn.execute(
            "SELECT id, source_id, observed_name, canonical_person_id, link_status "
            "FROM source_entities WHERE source_type IN ('vault', 'granola')"
        ):
            note_path, name = _note_path_and_name(source_id or "")
            notes[note_path].links.append(Link(link_id, note_path, observed or name, person_id, status))
    finally:
        conn.close()
    conn = _connect_read_only(interactions_db)
    try:
        for interaction_id, person_id, source_type, source_id in conn.execute(
            "SELECT id, person_id, source_type, source_id FROM interactions "
            "WHERE source_type IN ('vault', 'granola')"
        ):
            notes[source_id or ""].interactions.append(
                NoteInteraction(interaction_id, source_id or "", person_id, source_type)
            )
    finally:
        conn.close()
    return notes


def _read_note(vault: Path, rel_path: Optional[str]) -> Optional[tuple[dict, str]]:
    """A note's frontmatter and body, or None when it isn't on disk."""
    if rel_path is None:
        return None
    path = vault / rel_path
    try:
        return extract_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


# ---------------------------------------------------------------------- plan


class _FullNames:
    """Real people by full name, for mentions and co-mentions that give one."""

    def __init__(self, people: list[PersonEntity]):
        self._by_name: dict[str, Optional[PersonEntity]] = {}
        for person in people:
            if is_bare_person(person):
                continue
            for name in [person.canonical_name, *person.aliases]:
                key = name.strip().casefold()
                # A name two people share identifies neither.
                self._by_name[key] = None if key in self._by_name else person

    def get(self, name: str) -> Optional[PersonEntity]:
        return self._by_name.get(name.strip().casefold())


def _note_date(path: Path, frontmatter: dict, body: str):
    from api.services.indexer import _resolve_undated_note_date
    from api.services.interaction_store import UNDATED_SENTINEL

    text = extract_note_date(path, frontmatter, body)
    if text:
        return datetime.strptime(text, "%Y-%m-%d").date()
    resolved = _resolve_undated_note_date(path)
    return None if resolved == UNDATED_SENTINEL else resolved.date()


def _plan_no_people_note(rows: NoteRows, protected: dict, plan: Plan) -> None:
    for link in rows.links:
        if link.status != "auto":
            plan.counts["no_people_note_links_kept_human"] += 1
        elif link.person_id in protected:
            plan.counts["no_people_note_links_kept_protected"] += 1
        else:
            plan.delete_link_ids.append(link.id)
            plan.counts["no_people_note_links_removed"] += 1
            if link.person_id:
                plan.affected_ids.add(link.person_id)
    for interaction in rows.interactions:
        if interaction.person_id in protected:
            plan.counts["no_people_note_interactions_kept_protected"] += 1
        else:
            plan.delete_interaction_ids.append(interaction.id)
            plan.counts["no_people_note_interactions_removed"] += 1
            plan.affected_ids.add(interaction.person_id)


def _decide(
    name: str,
    context,
    resolver: MentionResolver,
    full_names: _FullNames,
) -> MentionDecision:
    if is_ambiguous_mention(name):
        return resolver.resolve(name, context)
    target = full_names.get(name)
    return MentionDecision(name, REASON_LINKED if target else "no_candidate", target, score=100.0 if target else 0.0)


def _plan_mentions(
    note_rows: list[NoteRows],
    rel_path: Optional[str],
    note: Optional[tuple[dict, str]],
    eligible: dict[str, PersonEntity],
    resolver: MentionResolver,
    full_names: _FullNames,
    vault: Path,
    tags_db: Optional[Path],
    employment: EmploymentHistory,
    plan: Plan,
) -> None:
    """Decide each eligible mention in one note."""
    links: dict[tuple[str, str], list[Link]] = defaultdict(list)
    held: dict[str, list[NoteInteraction]] = defaultdict(list)
    co_mentioned: set[str] = set()
    for rows in note_rows:
        for link in rows.links:
            if link.person_id in eligible and link.status == "auto":
                links[(link.person_id, link.name)].append(link)
            elif link.person_id and link.person_id not in eligible and not is_ambiguous_mention(link.name):
                co_mentioned.add(link.person_id)
        for interaction in rows.interactions:
            if interaction.person_id in eligible:
                held[interaction.person_id].append(interaction)
    for person_id in held:
        if not any(key[0] == person_id for key in links):
            links[(person_id, eligible[person_id].canonical_name)] = []
    if not links:
        return

    context = None
    if note is not None:
        frontmatter, body = note
        for name in people_in_note(frontmatter, body):
            if not is_ambiguous_mention(name):
                person = full_names.get(name)
                if person:
                    co_mentioned.add(person.id)
        context = build_note_context(
            rel_path, frontmatter, body,
            note_date=_note_date(vault / rel_path, frontmatter, body),
            tag_domain=load_tag_domain(tags_db, rel_path),
            employment=employment,
            co_mentioned_ids=co_mentioned,
            is_granola="granola_id" in frontmatter or "wispr_id" in frontmatter or "Granola" in rel_path,
        )

    for (person_id, name), mention_links in sorted(links.items()):
        interactions = held.get(person_id, [])
        # One decision per row and note, even if the row holds several spellings.
        decision = _decide(name, context, resolver, full_names) if context else None
        if decision is not None and decision.entity is not None:
            source_type = interactions[0].source_type if interactions else "vault"
            note_path = (mention_links[0].note_path if mention_links else interactions[0].note_path)
            plan.moves.append(Move(
                from_id=person_id, to_id=decision.entity.id, mention=name, rel_path=rel_path,
                note_path=note_path, source_type=source_type, decision=decision,
                link_ids=[link.id for link in mention_links],
                interaction_ids=[i.id for i in interactions],
            ))
            held[person_id] = []
            plan.counts["mentions_moved"] += 1
            plan.counts["links_moved"] += len(mention_links)
            plan.counts["interactions_moved"] += len(interactions)
            plan.affected_ids.update((person_id, decision.entity.id))
        else:
            plan.unlink_ids.extend(link.id for link in mention_links)
            plan.delete_interaction_ids.extend(i.id for i in interactions)
            held[person_id] = []
            plan.counts["mentions_unlinked"] += 1
            plan.counts["links_removed"] += len(mention_links)
            plan.counts["interactions_removed"] += len(interactions)
            reason = decision.reason if decision else "note_not_on_disk"
            plan.counts[f"unlinked:{reason}"] += 1
            plan.affected_ids.add(person_id)
            plan.unlink_samples.append({
                "note": rel_path, "mention": name, "reason": reason,
                "best_score": decision.score if decision else None,
                "lead": decision.lead if decision else None,
            })


def _group_by_note(notes: dict[str, NoteRows], vault: Path) -> dict[Optional[str], list[NoteRows]]:
    """Group rows stored under any of a note's locations by its vault-relative path."""
    grouped: dict[Optional[str], list[NoteRows]] = defaultdict(list)
    for note_path, rows in notes.items():
        grouped[vault_relative_path(note_path, vault)].append(rows)
    return grouped


def build_plan(
    crm_db: Path,
    interactions_db: Path,
    vault: Path,
    tags_db: Optional[Path],
    merged_ids_path: Path,
    employment: Optional[EmploymentHistory] = None,
    no_people_paths: Optional[list[str]] = None,
) -> Plan:
    """Work out the whole repair from read-only connections."""
    employment = employment if employment is not None else get_employment_history()
    no_people_paths = no_people_paths if no_people_paths is not None else settings.vault_no_people_paths
    merged_ids = load_merged_ids(merged_ids_path)
    people = load_people(crm_db, merged_ids)
    bare = {p.id: p for p in people if is_bare_person(p)}
    evidence = _load_evidence(crm_db, interactions_db, set(bare))

    plan = Plan()
    plan.counts["people_visible"] = len(people)
    plan.counts["bare_rows"] = len(bare)
    for person in bare.values():
        reasons = protection_reasons(person, evidence)
        if reasons:
            plan.protected[person.id] = reasons
    eligible = {pid: p for pid, p in bare.items() if pid not in plan.protected}
    plan.counts["bare_rows_protected"] = len(plan.protected)
    plan.counts["bare_rows_eligible"] = len(eligible)
    for reasons in plan.protected.values():
        for reason in reasons:
            plan.counts[f"protected:{reason}"] += 1

    resolver = MentionResolver(
        people,
        crm_db=crm_db,
        interaction_db=interactions_db,
        vault_root=vault,
        overrides=load_link_overrides(crm_db),
        employment=employment,
        my_person_id=settings.my_person_id or None,
        owner_name=settings.user_name or None,
    )
    full_names = _FullNames(people)

    notes = _load_note_rows(crm_db, interactions_db)
    for rel_path, note_rows in sorted(_group_by_note(notes, vault).items(), key=lambda kv: kv[0] or ""):
        note = _read_note(vault, rel_path)
        reason = "underscore_dir" if rel_path and in_underscore_dir(rel_path) else no_people_reason(
            rel_path, note[0] if note else {}, no_people_paths
        )
        if reason:
            plan.no_people_notes[reason] += 1
            for rows in note_rows:
                _plan_no_people_note(rows, plan.protected, plan)
            # Rows left on protected people stay; eligible rows are done.
            continue
        _plan_mentions(
            note_rows, rel_path, note, eligible, resolver, full_names,
            vault, tags_db, employment, plan,
        )

    plan.hide_ids = sorted(eligible)
    plan.counts["bare_rows_to_hide"] = len(plan.hide_ids)
    plan.counts["people_visible_after"] = len(people) - len(plan.hide_ids)
    plan.counts["move_targets"] = len({m.to_id for m in plan.moves})
    for reason, count in plan.no_people_notes.items():
        plan.counts[f"no_people_notes:{reason}"] = count
    return plan


# --------------------------------------------------------------------- apply


def backup_databases(crm_db: Path, interactions_db: Path) -> list[Path]:
    """Copy both databases aside with SQLite's backup API, private to the owner."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    written = []
    for db in (crm_db, interactions_db):
        target = db.with_name(f"{db.name}.bak-mention-repair-{stamp}")
        source = sqlite3.connect(str(db), timeout=60.0)
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        os.chmod(target, 0o600)
        written.append(target)
    return written


def _in_chunks(items: list[str]):
    for i in range(0, len(items), _CHUNK):
        yield items[i:i + _CHUNK]


def _apply_rows(plan: Plan, crm_db: Path, interactions_db: Path) -> None:
    conn = sqlite3.connect(str(interactions_db), timeout=60.0)
    try:
        for move in plan.moves:
            conn.executemany(
                "UPDATE interactions SET person_id = ? WHERE id = ?",
                [(move.to_id, interaction_id) for interaction_id in move.interaction_ids],
            )
        for chunk in _in_chunks(plan.delete_interaction_ids):
            conn.execute(f"DELETE FROM interactions WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        conn.commit()
    finally:
        conn.close()

    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(crm_db), timeout=60.0)
    try:
        for move in plan.moves:
            conn.executemany(
                "UPDATE source_entities SET canonical_person_id = ?, link_confidence = ?, "
                "link_method = ?, linked_at = ? WHERE id = ?",
                [
                    (move.to_id, move.decision.confidence, move.decision.match_type or MENTION_MATCH_TYPE, now, link_id)
                    for link_id in move.link_ids
                ],
            )
        for chunk in _in_chunks(plan.unlink_ids):
            conn.execute(
                "UPDATE source_entities SET canonical_person_id = NULL, link_confidence = 0, "
                f"link_method = NULL, linked_at = NULL WHERE id IN ({','.join('?' * len(chunk))})",
                chunk,
            )
        for chunk in _in_chunks(plan.delete_link_ids):
            conn.execute(f"DELETE FROM source_entities WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        conn.commit()
    finally:
        conn.close()


def _apply_people(plan: Plan, store: PersonEntityStore, crm_db: Path) -> None:
    """Credit each target with the notes that moved to them, then hide empty rows."""
    by_target: dict[str, list[Move]] = defaultdict(list)
    for move in plan.moves:
        by_target[move.to_id].append(move)
    for target_id, moves in by_target.items():
        person = store.get_by_id(target_id)
        if not person:
            continue
        for move in moves:
            if move.interaction_ids:
                if move.note_path not in person.related_notes:
                    person.related_notes.append(move.note_path)
                if move.source_type not in person.sources:
                    person.sources.append(move.source_type)
        store.update(person)

    for person_id in plan.hide_ids:
        store.hide_person(person_id, HIDDEN_REASON)

    conn = sqlite3.connect(str(crm_db), timeout=60.0)
    try:
        counts = dict(conn.execute(
            "SELECT canonical_person_id, COUNT(*) FROM source_entities "
            "WHERE canonical_person_id IS NOT NULL GROUP BY canonical_person_id"
        ))
    finally:
        conn.close()
    for person_id in plan.affected_ids | set(plan.hide_ids):
        person = store.get_by_id(person_id)
        if person and person.source_entity_count != counts.get(person.id, 0):
            person.source_entity_count = counts.get(person.id, 0)
            store.update(person)


def execute_plan(plan: Plan, crm_db: Path, interactions_db: Path, merged_ids_path: Path) -> dict:
    """Apply a plan, then recompute stats for everyone it touched."""
    _apply_rows(plan, crm_db, interactions_db)

    # person_stats reads the singleton store and the settings-derived path, so
    # both have to name the databases this run was pointed at.
    import api.services.interaction_store as interaction_store
    import api.services.person_entity as person_entity
    from api.services.person_stats import refresh_person_stats

    PersonEntityStore.MERGED_IDS_PATH = merged_ids_path
    interaction_store.get_interaction_db_path = lambda: str(interactions_db)
    person_entity._entity_store = None
    store = person_entity.get_person_entity_store(str(crm_db))
    assert Path(store.db_path).resolve() == crm_db.resolve()

    _apply_people(plan, store, crm_db)
    return refresh_person_stats(sorted(plan.affected_ids | set(plan.hide_ids)))


# -------------------------------------------------------------------- report


def _move_sample(move: Move) -> dict:
    runner_up = move.decision.candidates[1] if len(move.decision.candidates) > 1 else None
    winner = move.decision.candidates[0] if move.decision.candidates else None
    return {
        "note": move.rel_path,
        "mention": move.mention,
        "chosen_person": move.decision.entity.canonical_name,
        "chosen_person_id": move.to_id,
        "score": move.decision.score,
        "lead": move.decision.lead,
        "signals": winner.signals if winner else {move.decision.reason: move.decision.score},
        "candidates": len(move.decision.candidates),
        "runner_up": runner_up.entity.canonical_name if runner_up else None,
        "runner_up_score": runner_up.score if runner_up else None,
        "runner_up_signals": runner_up.signals if runner_up else None,
    }


def write_report(plan: Plan, report_path: Path, people_by_id: dict[str, PersonEntity]) -> None:
    """Write the private report. Refuses a path inside the repository."""
    resolved = report_path.resolve()
    if REPO_ROOT in resolved.parents:
        raise SystemExit("The report holds private data and must stay outside the repository")
    resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    rng = random.Random(SAMPLE_SEED)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": dict(plan.counts),
        "signal_totals_on_moves": dict(sum(
            (Counter(m.decision.candidates[0].signals.keys()) for m in plan.moves if m.decision.candidates),
            Counter(),
        )),
        "moved_sample": [_move_sample(m) for m in rng.sample(plan.moves, min(SAMPLE_SIZE, len(plan.moves)))],
        "unlinked_sample": rng.sample(plan.unlink_samples, min(SAMPLE_SIZE, len(plan.unlink_samples))),
        "protected_rows_for_review": [
            {
                "person_id": person_id,
                "name": people_by_id[person_id].canonical_name if person_id in people_by_id else None,
                "reasons": reasons,
            }
            for person_id, reasons in sorted(plan.protected.items())
        ],
        "hidden_rows": [
            people_by_id[person_id].canonical_name if person_id in people_by_id else person_id
            for person_id in plan.hide_ids
        ],
    }
    descriptor = os.open(str(resolved), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as f:
        json.dump(report, f, indent=2, default=str)
    os.chmod(resolved, 0o600)


def _print_counts(plan: Plan) -> None:
    for key in sorted(plan.counts):
        print(f"  {key}: {plan.counts[key]}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-resolve note mentions on one-word people and hide the rows left empty"
    )
    parser.add_argument("--crm-db", required=True, type=Path)
    parser.add_argument("--interactions-db", required=True, type=Path)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--vault-tags-db", type=Path, help="Tagged domains for notes, when available")
    parser.add_argument("--merged-ids", type=Path, help="Default: merged_person_ids.json beside the CRM database")
    parser.add_argument("--report", type=Path, help="Private report path, outside the repository")
    parser.add_argument("--execute", action="store_true", help="Back up, then apply")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    merged_ids_path = args.merged_ids or args.crm_db.with_name("merged_person_ids.json")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = args.report or DEFAULT_REPORT_DIR / f"report-{stamp}.json"

    plan = build_plan(args.crm_db, args.interactions_db, args.vault, args.vault_tags_db, merged_ids_path)
    people_by_id = {p.id: p for p in load_people(args.crm_db, load_merged_ids(merged_ids_path))}

    print("EXECUTE" if args.execute else "DRY RUN (use --execute to apply)")
    _print_counts(plan)
    write_report(plan, report_path, people_by_id)
    print(f"Private report: {report_path}")

    if args.execute:
        backups = backup_databases(args.crm_db, args.interactions_db)
        print(f"Backed up {len(backups)} databases beside the originals")
        result = execute_plan(plan, args.crm_db, args.interactions_db, merged_ids_path)
        print(f"Stats refreshed for {result.get('updated', 0)} people, "
              f"last_seen changed on {result.get('last_seen_changed', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
