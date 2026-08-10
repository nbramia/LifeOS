#!/usr/bin/env python3
"""
Post-Sync Entity Cleanup Script.

Runs after nightly sync to auto-hide obvious non-human entities
(noreply@, newsletters, marketing senders) using rule-based detection,
so they never surface in the CRM people list.

This script is designed to be run as part of the nightly sync pipeline
(Phase 6 in run_all_syncs.py).

Usage:
    python scripts/sync_entity_cleanup.py [--dry-run] [--execute]

Options:
    --dry-run   Show what would be done without making changes (default)
    --execute   Actually hide entities
"""
import argparse
import logging
import re
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.person_entity import PersonEntity, get_person_entity_store

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


# =============================================================================
# Rule-Based Non-Human Detection
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
        queue_for_llm_list: Ambiguous entities (caller discards these — they
            are left untouched, neither hidden nor queued)
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
            # Check if name looks like an email address (ambiguous)
            if is_email_address(name):
                queue_for_llm.append((entity, 0.70, "Name appears to be an email address"))
                logger.debug(f"Ambiguous (email-as-name): {name}")
                continue

            # Check for very short names (ambiguous)
            if len(name.strip()) < 3:
                queue_for_llm.append((entity, 0.70, "Very short name"))
                logger.debug(f"Ambiguous (short name): {name}")
                continue

            # Check for ALL CAPS names with > 4 chars (ambiguous)
            if len(name) > 4 and name.isupper():
                queue_for_llm.append((entity, 0.60, "All caps name"))
                logger.debug(f"Ambiguous (all caps): {name}")
                continue

    logger.info(f"Rule-based detection: {len(auto_hide)} auto-hide, "
               f"{len(queue_for_llm)} ambiguous (left untouched)")
    return auto_hide, queue_for_llm


# =============================================================================
# Main Cleanup Orchestration
# =============================================================================

def run_cleanup(dry_run: bool = True) -> dict:
    """
    Run the entity cleanup process: rule-based auto-hide of obvious
    non-human entities.

    Args:
        dry_run: If True, don't actually hide entities

    Returns:
        Statistics dict
    """
    logger.info(f"Starting entity cleanup (dry_run={dry_run})")

    person_store = get_person_entity_store()

    # Get all entities
    entities = person_store.get_all(include_hidden=False)
    logger.info(f"Processing {len(entities)} entities")

    stats = {
        "total_entities": len(entities),
        "auto_hidden": 0,
    }

    # Rule-based non-human detection. Ambiguous entities are discarded —
    # left untouched (not hidden, not queued).
    logger.info("Rule-based non-human detection")
    auto_hide_list, _ = detect_non_humans_rule_based(entities, dry_run)

    # Apply changes
    if not dry_run:
        for entity in auto_hide_list:
            try:
                person_store.hide_person(entity.id, reason="Auto-classified as non-human")
                stats["auto_hidden"] += 1
            except Exception as e:
                logger.error(f"Failed to hide {entity.canonical_name}: {e}")

        # Save person store if we made changes
        if stats["auto_hidden"] > 0:
            person_store.save()
    else:
        # Dry run - just count
        stats["auto_hidden"] = len(auto_hide_list)

    # Log summary
    logger.info("=" * 60)
    logger.info("ENTITY CLEANUP COMPLETE")
    logger.info(f"  Total entities: {stats['total_entities']}")
    logger.info(f"  Auto-hidden: {stats['auto_hidden']}")
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

        # Canonical line consumed by run_all_syncs._parse_sync_output. The
        # bare "created:/updated:" prints below were written for the original
        # parser; commit 1a753b1 (2026-02-11) removed those generic patterns
        # and this script silently reported 0/0/0 from that night on, while
        # still doing ~600s of real work for months (#497).
        from api.services.sync_health import emit_sync_stats
        emit_sync_stats({
            "processed": int(stats.get("total_entities", 0) or 0),
            "updated": int(stats.get("auto_hidden", 0) or 0),
        })

        # Human-readable summary (no longer load-bearing for stats).
        print(f"processed: {stats['total_entities']}")
        print(f"updated: {stats['auto_hidden']}")

        return 0

    except Exception as e:
        logger.exception(f"Cleanup failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
