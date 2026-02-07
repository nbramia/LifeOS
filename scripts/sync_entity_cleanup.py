#!/usr/bin/env python3
"""
Post-Sync Entity Cleanup Script.

Runs after nightly sync to:
1. Auto-hide obvious non-human entities (rule-based)
2. Queue ambiguous entities for LLM classification
3. Detect duplicate candidates and queue for review

This script is designed to be run as part of the nightly sync pipeline
(Phase 6 in run_all_syncs.py).

Usage:
    python scripts/sync_entity_cleanup.py [--dry-run] [--execute]

Options:
    --dry-run   Show what would be done without making changes (default)
    --execute   Actually hide entities and add to review queue
"""
import argparse
import asyncio
import logging
import re
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.person_entity import PersonEntity, get_person_entity_store
from api.services.review_queue import get_review_queue_store, ReviewType

logger = logging.getLogger(__name__)

# =============================================================================
# Non-Human Detection Patterns
# =============================================================================

# Patterns that indicate a non-human entity (high confidence auto-hide)
NOREPLY_PATTERNS = [
    r"noreply",
    r"no-reply",
    r"no\.reply",
    r"donotreply",
    r"do-not-reply",
    r"do\.not\.reply",
    r"notification",
    r"notifications",
    r"mailer-daemon",
    r"mailerdaemon",
    r"postmaster",
    r"bounce",
    r"bounces",
    r"daemon",
    r"system",
    r"automated",
    r"auto-reply",
    r"autoreply",
]

# Email prefix patterns that suggest marketing/service accounts
MARKETING_EMAIL_PREFIXES = [
    "newsletter",
    "news",
    "updates",
    "billing",
    "invoice",
    "invoices",
    "receipt",
    "receipts",
    "order",
    "orders",
    "shipping",
    "delivery",
    "support",
    "help",
    "info",
    "contact",
    "sales",
    "marketing",
    "promo",
    "promotions",
    "deals",
    "offers",
    "subscription",
    "subscriptions",
    "confirm",
    "confirmation",
    "verify",
    "verification",
    "security",
    "alert",
    "alerts",
    "account",
    "accounts",
    "service",
    "services",
    "team",
    "hello",
    "hi",
    "hey",
]

# Compiled regex for noreply patterns
NOREPLY_REGEX = re.compile(
    r"|".join(NOREPLY_PATTERNS),
    re.IGNORECASE
)


def is_email_address(name: str) -> bool:
    """Check if a name looks like an email address."""
    if not name:
        return False
    return "@" in name and "." in name.split("@")[-1]


def extract_email_username(email: str) -> str:
    """Extract username part from an email address."""
    if "@" not in email:
        return email.lower()
    return email.split("@")[0].lower()


def parse_username_to_name_parts(username: str) -> list[tuple[str, str]]:
    """
    Parse a username into potential name parts.

    Examples:
        "nbramia" -> [("n", "bramia"), ("nb", "ramia"), ...]
        "john.doe" -> [("john", "doe")]
        "jsmith123" -> [("j", "smith")]

    Returns list of (first_part, last_part) tuples.
    """
    # Remove numbers at the end
    clean = re.sub(r"\d+$", "", username)

    # Try common separators first
    for sep in [".", "_", "-"]:
        if sep in clean:
            parts = clean.split(sep)
            if len(parts) == 2 and len(parts[0]) >= 1 and len(parts[1]) >= 2:
                return [(parts[0], parts[1])]

    # Try to split on camelCase
    camel_parts = re.findall(r"[A-Z]?[a-z]+", clean)
    if len(camel_parts) == 2:
        return [(camel_parts[0].lower(), camel_parts[1].lower())]

    # Try single initial + rest
    results = []
    if len(clean) >= 3:
        # Single initial: n + bramia
        results.append((clean[0], clean[1:]))
        # Double initial: nb + ramia
        if len(clean) >= 4:
            results.append((clean[:2], clean[2:]))

    return results


def normalize_name(name: str) -> str:
    """Normalize a name for comparison (lowercase, stripped, no extra spaces)."""
    if not name:
        return ""
    return " ".join(name.lower().split())


def fuzzy_name_match(name1: str, name2: str) -> tuple[bool, float]:
    """
    Check if two names might match.

    Returns (is_match, confidence) where confidence is 0-1.
    """
    n1 = normalize_name(name1)
    n2 = normalize_name(name2)

    if not n1 or not n2:
        return False, 0.0

    # Exact match
    if n1 == n2:
        return True, 1.0

    # Split into parts
    parts1 = n1.split()
    parts2 = n2.split()

    # Check if one is initial + last matching other's first + last
    # e.g., "J Smith" matches "John Smith"
    if len(parts1) == 2 and len(parts2) == 2:
        # Check first initial match
        if len(parts1[0]) == 1 and parts2[0].startswith(parts1[0]):
            if parts1[1] == parts2[1]:
                return True, 0.8

        if len(parts2[0]) == 1 and parts1[0].startswith(parts2[0]):
            if parts1[1] == parts2[1]:
                return True, 0.8

    # Check for last name only match (weak signal)
    if len(parts1) >= 1 and len(parts2) >= 1:
        if parts1[-1] == parts2[-1] and len(parts1[-1]) > 3:
            return True, 0.5

    return False, 0.0


def check_email_username_match(
    email_name: str,
    candidate_name: str
) -> tuple[bool, float, str]:
    """
    Check if an email-as-name might match a real person name.

    Args:
        email_name: The email address used as a name (e.g., "user@gmail.com")
        candidate_name: A real person name to compare (e.g., "Nathan Ramia")

    Returns:
        (is_match, confidence, match_reason)
    """
    username = extract_email_username(email_name)
    name_parts_list = parse_username_to_name_parts(username)

    candidate_parts = normalize_name(candidate_name).split()
    if len(candidate_parts) < 2:
        return False, 0.0, ""

    candidate_first = candidate_parts[0]
    candidate_last = candidate_parts[-1]

    for first_part, last_part in name_parts_list:
        # Check if last_part matches candidate last name (or prefix)
        if candidate_last.startswith(last_part) or last_part.startswith(candidate_last):
            # Check if first_part is initial or prefix of first name
            if candidate_first.startswith(first_part) or first_part.startswith(candidate_first[0]):
                confidence = 0.7
                if len(first_part) > 1 and candidate_first.startswith(first_part):
                    confidence = 0.85
                return True, confidence, f"Username '{username}' matches '{candidate_name}'"

    return False, 0.0, ""


# =============================================================================
# Phase 1: Rule-Based Non-Human Detection
# =============================================================================

def detect_non_humans_rule_based(
    entities: list[PersonEntity],
    dry_run: bool = True,
) -> tuple[list[PersonEntity], list[tuple[PersonEntity, float, str]]]:
    """
    Detect non-human entities using rule-based patterns.

    Returns:
        (auto_hide_list, queue_for_llm_list)

        auto_hide_list: Entities to auto-hide (high confidence)
        queue_for_llm_list: Entities to queue for LLM review with (entity, confidence, reason)
    """
    auto_hide = []
    queue_for_llm = []

    for entity in entities:
        if entity.hidden:
            continue

        name = entity.canonical_name or ""

        # Check for noreply patterns (0.95 confidence - auto-hide)
        if NOREPLY_REGEX.search(name):
            auto_hide.append(entity)
            logger.debug(f"Auto-hide (noreply): {name}")
            continue

        # Check for marketing email prefixes (0.90 confidence - auto-hide)
        name_lower = name.lower()
        for prefix in MARKETING_EMAIL_PREFIXES:
            if name_lower.startswith(prefix + "@") or name_lower.startswith(prefix + " "):
                auto_hide.append(entity)
                logger.debug(f"Auto-hide (marketing prefix): {name}")
                break
        else:
            # Check if name looks like an email address (queue for LLM)
            if is_email_address(name):
                queue_for_llm.append((entity, 0.70, "Name appears to be an email address"))
                logger.debug(f"Queue for LLM (email-as-name): {name}")
                continue

            # Check for very short names (queue for LLM)
            if len(name.strip()) < 3:
                queue_for_llm.append((entity, 0.70, "Very short name"))
                logger.debug(f"Queue for LLM (short name): {name}")
                continue

            # Check for ALL CAPS names with > 4 chars (queue for LLM)
            if len(name) > 4 and name.isupper():
                queue_for_llm.append((entity, 0.60, "All caps name"))
                logger.debug(f"Queue for LLM (all caps): {name}")
                continue

    logger.info(f"Rule-based detection: {len(auto_hide)} auto-hide, "
               f"{len(queue_for_llm)} for LLM review")
    return auto_hide, queue_for_llm


# =============================================================================
# Phase 2: LLM Classification for Ambiguous Cases
# =============================================================================

async def classify_with_llm(
    entities: list[tuple[PersonEntity, float, str]],
    batch_size: int = 10,
) -> tuple[list[PersonEntity], list[tuple[PersonEntity, float, str]]]:
    """
    Use LLM to classify ambiguous entities.

    Returns:
        (auto_hide_list, queue_for_manual_review_list)
    """
    if not entities:
        return [], []

    try:
        from api.services.ollama_client import OllamaClient, OllamaError

        client = OllamaClient()

        # Check if LLM is available
        if not client.is_available():
            logger.warning("Ollama not available, queueing all ambiguous entities for manual review")
            return [], entities

        auto_hide = []
        queue_for_manual = []

        # Process in batches
        for i in range(0, len(entities), batch_size):
            batch = entities[i:i + batch_size]

            # Build prompt
            names_list = "\n".join([
                f"- {entity.canonical_name}"
                for entity, _, _ in batch
            ])

            prompt = f"""You are classifying entity names in a personal CRM system.
For each name, determine if it is:
- "human": A real person's name
- "non_human": A service, bot, company, or automated account

Names to classify:
{names_list}

Respond with a JSON object mapping each name to its classification and confidence (0.0-1.0).
Example:
{{"john.smith@example.com": {{"type": "human", "confidence": 0.9}},
 "newsletter@company.com": {{"type": "non_human", "confidence": 0.95}}}}

Only respond with the JSON object, no other text."""

            try:
                result = await client.generate_json(prompt, timeout=60)

                for entity, orig_confidence, reason in batch:
                    name = entity.canonical_name
                    classification = result.get(name, {})
                    entity_type = classification.get("type", "unknown")
                    confidence = classification.get("confidence", 0.5)

                    if entity_type == "non_human" and confidence >= 0.7:
                        auto_hide.append(entity)
                        logger.debug(f"LLM classified as non-human: {name} (confidence: {confidence})")
                    else:
                        # Queue for manual review
                        queue_for_manual.append((entity, confidence, f"{reason} (LLM: {entity_type})"))
                        logger.debug(f"LLM uncertain: {name} (type: {entity_type}, confidence: {confidence})")

            except OllamaError as e:
                logger.warning(f"LLM classification failed for batch: {e}")
                # Queue entire batch for manual review
                queue_for_manual.extend(batch)

        logger.info(f"LLM classification: {len(auto_hide)} auto-hide, "
                   f"{len(queue_for_manual)} for manual review")
        return auto_hide, queue_for_manual

    except ImportError:
        logger.warning("Ollama client not available, queueing all for manual review")
        return [], entities


# =============================================================================
# Phase 3: Duplicate Detection
# =============================================================================

def detect_duplicates(
    entities: list[PersonEntity],
) -> list[tuple[PersonEntity, PersonEntity, float, str, dict]]:
    """
    Detect potential duplicate entities.

    Returns list of (entity_a, entity_b, confidence, reason, evidence) tuples.
    """
    duplicates = []

    # Build indices for fast lookup
    email_to_entities: dict[str, list[PersonEntity]] = defaultdict(list)
    phone_to_entities: dict[str, list[PersonEntity]] = defaultdict(list)
    name_to_entities: dict[str, list[PersonEntity]] = defaultdict(list)

    for entity in entities:
        if entity.hidden:
            continue

        for email in entity.emails:
            email_to_entities[email.lower()].append(entity)

        for phone in entity.phone_numbers:
            phone_to_entities[phone].append(entity)

        norm_name = normalize_name(entity.canonical_name)
        if norm_name:
            name_to_entities[norm_name].append(entity)

    seen_pairs = set()

    # Check for shared emails
    for email, entities_with_email in email_to_entities.items():
        if len(entities_with_email) > 1:
            for i, entity_a in enumerate(entities_with_email):
                for entity_b in entities_with_email[i + 1:]:
                    pair_key = tuple(sorted([entity_a.id, entity_b.id]))
                    if pair_key not in seen_pairs:
                        seen_pairs.add(pair_key)
                        duplicates.append((
                            entity_a, entity_b,
                            0.95,
                            f"Shared email: {email}",
                            {"shared_email": email}
                        ))

    # Check for shared phones
    for phone, entities_with_phone in phone_to_entities.items():
        if len(entities_with_phone) > 1:
            for i, entity_a in enumerate(entities_with_phone):
                for entity_b in entities_with_phone[i + 1:]:
                    pair_key = tuple(sorted([entity_a.id, entity_b.id]))
                    if pair_key not in seen_pairs:
                        seen_pairs.add(pair_key)
                        duplicates.append((
                            entity_a, entity_b,
                            0.90,
                            f"Shared phone: {phone}",
                            {"shared_phone": phone}
                        ))

    # Check for same normalized name (different IDs)
    for name, entities_with_name in name_to_entities.items():
        if len(entities_with_name) > 1:
            for i, entity_a in enumerate(entities_with_name):
                for entity_b in entities_with_name[i + 1:]:
                    pair_key = tuple(sorted([entity_a.id, entity_b.id]))
                    if pair_key not in seen_pairs:
                        seen_pairs.add(pair_key)
                        duplicates.append((
                            entity_a, entity_b,
                            0.70,
                            f"Same name: {name}",
                            {"normalized_name": name}
                        ))

    # Check for email-username matching
    email_name_entities = [e for e in entities if not e.hidden and is_email_address(e.canonical_name)]
    real_name_entities = [e for e in entities if not e.hidden and not is_email_address(e.canonical_name)]

    for email_entity in email_name_entities:
        for real_entity in real_name_entities:
            pair_key = tuple(sorted([email_entity.id, real_entity.id]))
            if pair_key in seen_pairs:
                continue

            is_match, confidence, reason = check_email_username_match(
                email_entity.canonical_name,
                real_entity.canonical_name
            )

            if is_match:
                seen_pairs.add(pair_key)
                duplicates.append((
                    email_entity, real_entity,
                    confidence,
                    reason,
                    {"email_as_name": email_entity.canonical_name, "real_name": real_entity.canonical_name}
                ))

    logger.info(f"Duplicate detection: found {len(duplicates)} candidates")
    return duplicates


def detect_over_merged(
    entities: list[PersonEntity],
    source_threshold: int = 50,
    alias_threshold: int = 30,
) -> list[tuple[PersonEntity, float, str, dict]]:
    """
    Detect entities that may be over-merged (containing multiple people).

    Returns list of (entity, confidence, reason, evidence) tuples.
    """
    over_merged = []

    for entity in entities:
        if entity.hidden:
            continue

        evidence = {
            "source_count": len(entity.sources),
            "alias_count": len(entity.aliases),
            "email_count": len(entity.emails),
        }

        # Check for high source count
        if entity.source_entity_count >= source_threshold:
            over_merged.append((
                entity,
                0.60,
                f"Very high source count: {entity.source_entity_count} sources",
                evidence
            ))
            continue

        # Check for high alias count
        if len(entity.aliases) >= alias_threshold:
            over_merged.append((
                entity,
                0.60,
                f"Many aliases: {len(entity.aliases)} names",
                evidence
            ))
            continue

    logger.info(f"Over-merged detection: found {len(over_merged)} candidates")
    return over_merged


# =============================================================================
# Main Cleanup Orchestration
# =============================================================================

def run_cleanup(dry_run: bool = True) -> dict:
    """
    Run the full entity cleanup process.

    Args:
        dry_run: If True, don't actually hide entities or modify queue

    Returns:
        Statistics dict
    """
    logger.info(f"Starting entity cleanup (dry_run={dry_run})")

    person_store = get_person_entity_store()
    review_store = get_review_queue_store()

    # Generate batch ID for this run
    batch_id = f"cleanup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    # Get all entities
    entities = person_store.get_all(include_hidden=False)
    logger.info(f"Processing {len(entities)} entities")

    stats = {
        "total_entities": len(entities),
        "auto_hidden": 0,
        "queued_non_human": 0,
        "queued_duplicates": 0,
        "queued_over_merged": 0,
        "batch_id": batch_id,
    }

    # Phase 1: Rule-based non-human detection
    logger.info("Phase 1: Rule-based non-human detection")
    auto_hide_list, llm_queue = detect_non_humans_rule_based(entities, dry_run)

    # Phase 2: LLM classification for ambiguous cases
    logger.info("Phase 2: LLM classification")
    llm_auto_hide, manual_queue = asyncio.run(classify_with_llm(llm_queue))

    # Combine auto-hide lists
    all_auto_hide = auto_hide_list + llm_auto_hide

    # Phase 3: Duplicate and over-merged detection
    logger.info("Phase 3: Duplicate detection")
    duplicates = detect_duplicates(entities)

    logger.info("Phase 3b: Over-merged detection")
    over_merged = detect_over_merged(entities)

    # Apply changes
    if not dry_run:
        # Auto-hide non-human entities
        for entity in all_auto_hide:
            try:
                person_store.hide_person(entity.id, reason="Auto-classified as non-human")
                stats["auto_hidden"] += 1
            except Exception as e:
                logger.error(f"Failed to hide {entity.canonical_name}: {e}")

        # Queue non-humans for manual review
        for entity, confidence, reason in manual_queue:
            try:
                review_store.add_non_human(
                    person_id=entity.id,
                    person_name=entity.canonical_name,
                    confidence=confidence,
                    reason=reason,
                    evidence={"sources": entity.sources[:5]},
                    batch_id=batch_id,
                )
                stats["queued_non_human"] += 1
            except Exception as e:
                logger.error(f"Failed to queue non-human {entity.canonical_name}: {e}")

        # Queue duplicates for review
        for entity_a, entity_b, confidence, reason, evidence in duplicates:
            try:
                review_store.add_duplicate(
                    person_a_id=entity_a.id,
                    person_a_name=entity_a.canonical_name,
                    person_b_id=entity_b.id,
                    person_b_name=entity_b.canonical_name,
                    confidence=confidence,
                    reason=reason,
                    evidence=evidence,
                    batch_id=batch_id,
                )
                stats["queued_duplicates"] += 1
            except Exception as e:
                logger.error(f"Failed to queue duplicate: {e}")

        # Queue over-merged for review
        for entity, confidence, reason, evidence in over_merged:
            try:
                review_store.add_over_merged(
                    person_id=entity.id,
                    person_name=entity.canonical_name,
                    confidence=confidence,
                    reason=reason,
                    evidence=evidence,
                    batch_id=batch_id,
                )
                stats["queued_over_merged"] += 1
            except Exception as e:
                logger.error(f"Failed to queue over-merged: {e}")

        # Save person store if we made changes
        if stats["auto_hidden"] > 0:
            person_store.save()

    else:
        # Dry run - just count
        stats["auto_hidden"] = len(all_auto_hide)
        stats["queued_non_human"] = len(manual_queue)
        stats["queued_duplicates"] = len(duplicates)
        stats["queued_over_merged"] = len(over_merged)

    # Log summary
    logger.info("=" * 60)
    logger.info("ENTITY CLEANUP COMPLETE")
    logger.info(f"  Total entities: {stats['total_entities']}")
    logger.info(f"  Auto-hidden: {stats['auto_hidden']}")
    logger.info(f"  Queued non-human: {stats['queued_non_human']}")
    logger.info(f"  Queued duplicates: {stats['queued_duplicates']}")
    logger.info(f"  Queued over-merged: {stats['queued_over_merged']}")
    logger.info(f"  Batch ID: {stats['batch_id']}")
    if dry_run:
        logger.info("  (DRY RUN - no changes made)")
    logger.info("=" * 60)

    return stats


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Post-sync entity cleanup")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show what would be done without making changes")
    parser.add_argument("--execute", action="store_true",
                       help="Actually execute cleanup (required for non-dry-run)")
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )

    # Default to dry-run unless --execute is specified
    dry_run = not args.execute

    if dry_run:
        logger.info("Running in DRY RUN mode. Use --execute to make changes.")

    try:
        stats = run_cleanup(dry_run=dry_run)

        # Print summary for sync pipeline parsing
        print(f"processed: {stats['total_entities']}")
        print(f"created: {stats['queued_duplicates'] + stats['queued_non_human'] + stats['queued_over_merged']}")
        print(f"updated: {stats['auto_hidden']}")

        return 0

    except Exception as e:
        logger.exception(f"Cleanup failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
