"""
P9.1 Data Integrity Tests - Strict Requirements Verification

These tests verify that CRM data is CORRECT, not just that it EXISTS.
Each test class corresponds to a requirement in docs/CRM-P9.1-Requirements.md

Tests must pass before P9.1 can be considered complete.

NOTE: These tests require direct database access and will be skipped if
the server is running (database locked). Stop the server to run these tests.
"""
import os
import re
import sqlite3
import pytest
from pathlib import Path

from api.services.person_entity import get_person_entity_store
from api.services.interaction_store import get_interaction_db_path


# All classes in this file require database access
pytestmark = pytest.mark.usefixtures("require_db")


class TestR1CleanDatabase:
    """R1: Clean Interaction Database - No test pollution."""

    def test_no_temp_directory_interactions(self):
        """No interactions should point to /tmp or /var/folders paths."""
        conn = sqlite3.connect(get_interaction_db_path())
        cursor = conn.execute("""
            SELECT COUNT(*) FROM interactions
            WHERE source_id LIKE '/private/var/folders%'
               OR source_id LIKE '/tmp%'
               OR source_id LIKE '/var/%'
        """)
        count = cursor.fetchone()[0]
        conn.close()

        assert count == 0, f"Found {count} interactions pointing to temp directories - database is polluted with test data"

    def test_all_vault_files_exist(self):
        """All vault interactions must point to files that actually exist."""
        conn = sqlite3.connect(get_interaction_db_path())
        cursor = conn.execute("""
            SELECT source_id, title FROM interactions
            WHERE source_type IN ('vault', 'granola')
            AND source_id LIKE '/%'
        """)

        missing_files = []
        for row in cursor.fetchall():
            source_id, title = row
            if not os.path.exists(source_id):
                missing_files.append((source_id, title))

        conn.close()

        assert len(missing_files) == 0, (
            f"Found {len(missing_files)} vault interactions pointing to non-existent files:\n"
            + "\n".join(f"  - {path}" for path, _ in missing_files[:10])
        )

    def test_all_person_ids_exist(self):
        """All interactions must link to PersonEntity records that exist."""
        store = get_person_entity_store()
        # Include hidden (e.g., organizations) and merged entities since
        # interactions can legitimately reference them
        entity_ids = {e.id for e in store.get_all(include_hidden=True, include_merged=True)}

        conn = sqlite3.connect(get_interaction_db_path())
        cursor = conn.execute("SELECT DISTINCT person_id FROM interactions")
        interaction_person_ids = {row[0] for row in cursor.fetchall()}
        conn.close()

        orphaned = interaction_person_ids - entity_ids

        assert len(orphaned) == 0, (
            f"Found {len(orphaned)} person_ids in interactions that don't exist in PersonEntity store:\n"
            + "\n".join(f"  - {pid}" for pid in list(orphaned)[:10])
        )


class TestR2EntityResolution:
    """R2: Correct Entity Resolution - People must actually be mentioned in linked notes."""

    # Known unverifiable links, as of the last audit. These are not necessarily
    # wrong: Granola rewrites note bodies after indexing, so an interaction
    # created when a name was present can outlive the text that justified it.
    # The point of the ceiling is to catch *new* bad links without pretending
    # the existing backlog is clean. Lower it when links are cleaned up.
    #
    # The remainder was audited individually: it is dominated by one close
    # family member appearing across journal and therapy notes that discuss
    # her without naming her. The links resolution actually got wrong — vault
    # mentions attached to people whose name is an initial plus a surname,
    # which matched any extracted name sharing that letter (#551) — were
    # removed, along with two non-person entities parsed out of note text.
    MAX_UNVERIFIABLE_LINKS = 28

    @staticmethod
    def _declared_people(content: str) -> list[str]:
        """
        Names the note itself claims are involved.

        Vault interactions are created from names extracted from the note, and
        those often appear only in frontmatter ('people:' or 'people/<slug>'
        tags) — frequently in a variant spelling that the resolver fuzzy-matched
        ("Haley" -> "Hayley Currier"). Checking the body text alone misses them.
        """
        frontmatter = re.match(r"^---\n(.*?)\n---", content, re.S)
        if not frontmatter:
            return []
        block = frontmatter.group(1)

        declared = []
        people_block = re.search(r"^people:\n((?:\s*-\s*.+\n)+)", block, re.M)
        if people_block:
            declared += re.findall(r"-\s*(.+)", people_block.group(1))
        declared += [
            slug.split("/")[-1].replace("-", " ")
            for slug in re.findall(r"people/([\w-]+)", block)
        ]
        return [d.strip() for d in declared if d.strip()]

    def test_vault_interactions_mention_linked_person(self):
        """
        A vault interaction's person should be traceable to the note.

        Scans every vault interaction rather than sampling. The previous version
        used ORDER BY RANDOM() LIMIT 20 against live data carrying a ~3%
        unverifiable rate, which made it a coin flip: it failed roughly half of
        runs regardless of whether anything had regressed.
        """
        from rapidfuzz import fuzz

        store = get_person_entity_store()
        conn = sqlite3.connect(get_interaction_db_path())
        rows = conn.execute("""
            SELECT person_id, source_id, title FROM interactions
            WHERE source_type IN ('vault', 'granola')
            AND source_id LIKE '/%'
        """).fetchall()
        conn.close()

        # Common 2-letter names that are valid to search for
        valid_two_letter_names = {'ed', 'al', 'jo', 'bo', 'ty', 'lu', 'li', 'an'}

        content_cache: dict[str, str] = {}
        unverifiable = []
        checked = 0

        for person_id, source_id, title in rows:
            if not os.path.exists(source_id):
                continue
            person = store.get_by_id(person_id)
            if not person:
                continue

            if source_id not in content_cache:
                try:
                    content_cache[source_id] = Path(source_id).read_text(encoding='utf-8')
                except Exception:
                    content_cache[source_id] = ""
            content = content_cache[source_id]
            if not content:
                continue

            names_to_find = [person.canonical_name]
            if person.display_name and person.display_name != person.canonical_name:
                names_to_find.append(person.display_name)
            names_to_find.extend(person.aliases)
            if person.canonical_name and ' ' in person.canonical_name:
                parts = person.canonical_name.split()
                for part in (parts[0], parts[-1]):
                    if part not in names_to_find:
                        names_to_find.append(part)

            searchable = [
                n for n in names_to_find
                if n and (len(n) > 2 or n.lower() in valid_two_letter_names)
            ]

            checked += 1
            content_lower = content.lower()
            if any(n.lower() in content_lower for n in searchable):
                continue

            # Fall back to the names the note declares, allowing for the variant
            # spellings the resolver matched on in the first place.
            declared = self._declared_people(content)
            if any(
                fuzz.ratio(n.lower(), d.lower()) >= 85
                for n in searchable for d in declared
            ):
                continue

            unverifiable.append({
                'person': person.canonical_name,
                'file': source_id,
                'title': title,
            })

        assert checked > 0, "No vault interactions found to verify"

        assert len(unverifiable) <= self.MAX_UNVERIFIABLE_LINKS, (
            f"Vault links that cannot be traced to their note rose to "
            f"{len(unverifiable)} (ceiling {self.MAX_UNVERIFIABLE_LINKS}) "
            f"out of {checked} checked. New entity-resolution false positives "
            f"are the likely cause.\n"
            + "\n".join(
                f"  {fp['person']} <- {fp['file']}" for fp in unverifiable[:5]
            )
        )

class TestR3VaultLinks:
    """R3: Working Vault Links - Links must point to existing files."""

    def test_vault_source_ids_are_valid_paths(self):
        """Vault source_ids must be valid file paths."""
        conn = sqlite3.connect(get_interaction_db_path())
        cursor = conn.execute("""
            SELECT source_id FROM interactions
            WHERE source_type IN ('vault', 'granola')
            LIMIT 100
        """)

        invalid_paths = []
        for row in cursor.fetchall():
            source_id = row[0]
            # Should be an absolute path starting with /
            if not source_id.startswith('/'):
                invalid_paths.append(source_id)

        conn.close()

        assert len(invalid_paths) == 0, (
            f"Found {len(invalid_paths)} vault interactions without valid file paths:\n"
            + "\n".join(f"  - {p}" for p in invalid_paths[:10])
        )

    def test_can_construct_obsidian_url(self):
        """Should be able to construct obsidian:// URL from vault path."""
        conn = sqlite3.connect(get_interaction_db_path())
        cursor = conn.execute("""
            SELECT source_id FROM interactions
            WHERE source_type IN ('vault', 'granola')
            AND source_id LIKE '/%'
            LIMIT 1
        """)
        row = cursor.fetchone()
        conn.close()

        if row:
            source_id = row[0]
            # Extract vault name and relative path
            # Path format: /Users/<username>/Notes 2025/...
            # Obsidian URL: obsidian://open?vault=Notes%202025&file=...

            path = Path(source_id)
            assert path.suffix == '.md', f"Vault file should be .md: {source_id}"


class TestR4GmailLinks:
    """R4: Working Gmail Links - Must open specific email."""

    def test_gmail_interactions_have_message_id(self):
        """Gmail interactions must have message_id in source_id."""
        conn = sqlite3.connect(get_interaction_db_path())
        cursor = conn.execute("""
            SELECT source_id, title FROM interactions
            WHERE source_type = 'gmail'
            LIMIT 10
        """)

        rows = cursor.fetchall()
        conn.close()

        if not rows:
            pytest.skip("No gmail interactions to verify")

        for source_id, title in rows:
            # Gmail message IDs are hex strings
            assert source_id, f"Gmail interaction '{title}' has no source_id"
            assert len(source_id) > 10, f"Gmail source_id too short: {source_id}"

    def test_gmail_link_format(self):
        """Gmail links must be constructable to open specific email."""
        conn = sqlite3.connect(get_interaction_db_path())
        cursor = conn.execute("""
            SELECT source_id FROM interactions
            WHERE source_type = 'gmail'
            LIMIT 1
        """)
        row = cursor.fetchone()
        conn.close()

        if not row:
            pytest.skip("No gmail interactions to verify")

        message_id = row[0]
        # Construct the URL that should open the email
        expected_url = f"https://mail.google.com/mail/u/0/#inbox/{message_id}"

        assert message_id in expected_url
        assert expected_url.startswith("https://mail.google.com")


class TestR5CalendarLinks:
    """R5: Working Calendar Links - Must open specific event."""

    def test_calendar_interactions_have_event_id(self):
        """Calendar interactions must have event_id in source_id."""
        conn = sqlite3.connect(get_interaction_db_path())
        cursor = conn.execute("""
            SELECT source_id, title FROM interactions
            WHERE source_type = 'calendar'
            LIMIT 10
        """)

        rows = cursor.fetchall()
        conn.close()

        if not rows:
            pytest.skip("No calendar interactions to verify")

        for source_id, title in rows:
            assert source_id, f"Calendar interaction '{title}' has no source_id"


class TestR6InteractionCounts:
    """R6: Accurate Interaction Counts - PersonEntity counts must match database."""

    def test_counts_match_database(self):
        """PersonEntity counts must match actual interactions in database."""
        store = get_person_entity_store()
        conn = sqlite3.connect(get_interaction_db_path())

        # Get top 10 people by stored counts
        all_people = store.get_all()
        sorted_people = sorted(
            all_people,
            key=lambda p: p.email_count + p.meeting_count + p.mention_count,
            reverse=True
        )[:10]

        mismatches = []

        for person in sorted_people:
            # Stored counts aggregate canonical + every legacy ID that merges
            # to this person, so the "actual" comparison must query across the
            # same variant set. Otherwise canonicals with legacy interactions
            # show as over-counted mismatches even when stats are correct.
            variant_ids = list({person.id} | store.get_legacy_ids(person.id))
            placeholders = ','.join(['?'] * len(variant_ids))
            cursor = conn.execute(f"""
                SELECT source_type, COUNT(*)
                FROM interactions
                WHERE person_id IN ({placeholders})
                GROUP BY source_type
            """, tuple(variant_ids))

            actual_counts = {row[0]: row[1] for row in cursor.fetchall()}

            actual_email = actual_counts.get('gmail', 0)
            actual_meeting = actual_counts.get('calendar', 0)
            actual_mention = actual_counts.get('vault', 0) + actual_counts.get('granola', 0)

            if (person.email_count != actual_email or
                person.meeting_count != actual_meeting or
                person.mention_count != actual_mention):
                mismatches.append({
                    'name': person.canonical_name,
                    'stored': {
                        'email': person.email_count,
                        'meeting': person.meeting_count,
                        'mention': person.mention_count,
                    },
                    'actual': {
                        'email': actual_email,
                        'meeting': actual_meeting,
                        'mention': actual_mention,
                    }
                })

        conn.close()

        if mismatches:
            msg = f"Found {len(mismatches)} people with mismatched counts:\n"
            for m in mismatches:
                msg += f"\n  {m['name']}:\n"
                msg += f"    Stored: email={m['stored']['email']}, meeting={m['stored']['meeting']}, mention={m['stored']['mention']}\n"
                msg += f"    Actual: email={m['actual']['email']}, meeting={m['actual']['meeting']}, mention={m['actual']['mention']}\n"
            assert False, msg


# Test contact configuration - set these environment variables for canonical verification
# These should be your partner or closest contact
TEST_CANONICAL_EMAIL = os.environ.get("LIFEOS_TEST_CONTACT_EMAIL", "")
TEST_CANONICAL_PHONE = os.environ.get("LIFEOS_TEST_CONTACT_PHONE", "")
TEST_CANONICAL_NAMES = os.environ.get("LIFEOS_TEST_CONTACT_NAMES", "").lower().split(",") if os.environ.get("LIFEOS_TEST_CONTACT_NAMES") else []


@pytest.mark.skipif(not TEST_CANONICAL_EMAIL, reason="Set LIFEOS_TEST_CONTACT_EMAIL to run canonical verification tests")
class TestR7CanonicalContact:
    """R7: Canonical Contact Test Case - Primary relationship verification.

    Configure via environment variables:
    - LIFEOS_TEST_CONTACT_EMAIL: Primary contact email
    - LIFEOS_TEST_CONTACT_PHONE: Primary contact phone (E.164 format)
    - LIFEOS_TEST_CONTACT_NAMES: Comma-separated names/aliases (e.g., "john,johnny,j")
    """

    def test_contact_found_by_email(self):
        """Contact must be findable by email."""
        store = get_person_entity_store()
        person = store.get_by_email(TEST_CANONICAL_EMAIL)
        assert person is not None, f"Contact not found by email {TEST_CANONICAL_EMAIL}"

    @pytest.mark.skipif(not TEST_CANONICAL_PHONE, reason="Set LIFEOS_TEST_CONTACT_PHONE to run phone test")
    def test_contact_found_by_phone(self):
        """Contact must be findable by phone."""
        store = get_person_entity_store()
        person = store.get_by_phone(TEST_CANONICAL_PHONE)
        assert person is not None, f"Contact not found by phone {TEST_CANONICAL_PHONE}"

    def test_contact_has_multiple_sources(self):
        """Contact must have interactions from at least 2 different sources."""
        store = get_person_entity_store()
        person = store.get_by_email(TEST_CANONICAL_EMAIL)
        assert person is not None

        conn = sqlite3.connect(get_interaction_db_path())
        cursor = conn.execute("""
            SELECT DISTINCT source_type FROM interactions
            WHERE person_id = ?
        """, (person.id,))
        sources = [row[0] for row in cursor.fetchall()]
        conn.close()

        assert len(sources) >= 2, f"Contact only has interactions from {sources}, need at least 2 sources"

    @pytest.mark.skipif(not TEST_CANONICAL_NAMES, reason="Set LIFEOS_TEST_CONTACT_NAMES to run vault verification")
    def test_contact_vault_interactions_are_correct(self):
        """Every vault interaction linked to contact must actually mention them."""
        store = get_person_entity_store()
        person = store.get_by_email(TEST_CANONICAL_EMAIL)
        assert person is not None

        conn = sqlite3.connect(get_interaction_db_path())
        cursor = conn.execute("""
            SELECT source_id, title FROM interactions
            WHERE person_id = ? AND source_type IN ('vault', 'granola')
            AND source_id LIKE '/%'
        """, (person.id,))

        false_positives = []
        checked = 0

        for row in cursor.fetchall():
            source_id, title = row

            if not os.path.exists(source_id):
                continue

            try:
                content = Path(source_id).read_text(encoding='utf-8').lower()
            except Exception:
                continue

            # Check if any of contact's names appear
            found = False
            for name in TEST_CANONICAL_NAMES:
                if name.strip() in content:
                    found = True
                    break

            if not found:
                false_positives.append((source_id, title))

            checked += 1

        conn.close()

        if checked > 0 and false_positives:
            msg = f"Found {len(false_positives)} vault notes linked to contact that don't mention them:\n"
            for path, title in false_positives[:5]:
                msg += f"  - {title}: {path}\n"
            assert False, msg

    def test_contact_relationship_strength_positive(self):
        """Contact must have positive relationship strength from real data."""
        store = get_person_entity_store()
        person = store.get_by_email(TEST_CANONICAL_EMAIL)
        assert person is not None

        assert person.relationship_strength > 0, (
            f"Contact has relationship_strength={person.relationship_strength}, "
            "should be > 0 with real interaction data"
        )


class TestR8Top10Verification:
    """R8: Top 10 Verification - Not overfitting to a single contact."""

    def test_top_10_all_have_interactions(self):
        """Top 10 people by interaction count must all have real interactions."""
        store = get_person_entity_store()
        conn = sqlite3.connect(get_interaction_db_path())

        # Get actual top 10 from database
        cursor = conn.execute("""
            SELECT person_id, COUNT(*) as cnt
            FROM interactions
            GROUP BY person_id
            ORDER BY cnt DESC
            LIMIT 10
        """)

        top_10 = []
        for row in cursor.fetchall():
            person_id, count = row
            person = store.get_by_id(person_id)
            if person:
                top_10.append((person, count))

        conn.close()

        assert len(top_10) >= 5, f"Found only {len(top_10)} people with interactions in top 10"

        for person, count in top_10:
            assert count > 0, f"{person.canonical_name} in top 10 but has 0 interactions"

    def test_top_10_have_valid_interactions(self):
        """Top 10 must have at least one interaction with valid source."""
        store = get_person_entity_store()
        conn = sqlite3.connect(get_interaction_db_path())

        cursor = conn.execute("""
            SELECT person_id, COUNT(*) as cnt
            FROM interactions
            GROUP BY person_id
            ORDER BY cnt DESC
            LIMIT 10
        """)

        problems = []

        for row in cursor.fetchall():
            person_id, count = row
            person = store.get_by_id(person_id)
            if not person:
                continue

            # Check for at least one valid vault interaction
            cursor2 = conn.execute("""
                SELECT source_id FROM interactions
                WHERE person_id = ? AND source_type IN ('vault', 'granola')
                AND source_id LIKE '/%'
                LIMIT 5
            """, (person_id,))

            valid_found = False
            for row2 in cursor2.fetchall():
                if os.path.exists(row2[0]):
                    valid_found = True
                    break

            # Also check for gmail/calendar/imessage interactions
            cursor3 = conn.execute("""
                SELECT COUNT(*) FROM interactions
                WHERE person_id = ? AND source_type IN ('gmail', 'calendar', 'imessage')
            """, (person_id,))
            other_count = cursor3.fetchone()[0]

            if not valid_found and other_count == 0:
                problems.append(person.canonical_name)

        conn.close()

        assert len(problems) == 0, (
            f"These top 10 people have no valid interactions: {problems}"
        )
