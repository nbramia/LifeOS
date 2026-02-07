#!/usr/bin/env python3
"""
LinkedIn Profile Scraping - Phase 1: Browser Automation

This script is part of a two-phase LinkedIn profile scraping system:
  - Phase 1 (this script): Navigate profiles via browser, save HTML + photos
  - Phase 2 (extract_linkedin_data.py): Parse saved HTML to extract structured data

IMPORTANT: As of Feb 2026, actual scraping is done via Claude in Chrome MCP
rather than this script directly. This script provides:
  - Top N people selection by relationship strength
  - State management (tracking completed/pending profiles)
  - Memory monitoring to prevent OOM crashes

See docs/architecture/DATA-AND-SYNC.md for full documentation.

Data Files:
  - data/linkedin_scrape_state.json: Progress tracking
  - data/linkedin_extracted.json: Final extracted profile data
  - data/linkedin_profiles/: Raw HTML files (if saved)
  - data/linkedin_photos/: Profile photos (if downloaded)

Usage:
  python scripts/scrape_linkedin_profiles.py              # Dry run - show what would be scraped
  python scripts/scrape_linkedin_profiles.py --execute    # Initialize state file for scraping
  python scripts/scrape_linkedin_profiles.py --limit 100  # Limit to top 100 profiles

Note: Memory monitoring uses configurable thresholds to gracefully stop
before OOM crashes during long-running scrape sessions.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import json
import logging
import time
import random
from datetime import datetime, timezone

from api.utils.memory_monitor import MemoryMonitor

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Global memory monitor instance
memory_monitor = MemoryMonitor()

DATA_DIR = Path(__file__).parent.parent / "data"
PROFILES_DIR = DATA_DIR / "linkedin_profiles"
PHOTOS_DIR = DATA_DIR / "linkedin_photos"
STATE_FILE = DATA_DIR / "linkedin_scrape_state.json"
EXTRACTED_FILE = DATA_DIR / "linkedin_extracted.json"


def get_top_people_with_linkedin(limit: int = 300) -> list:
    """Get top N people by relationship_strength who have linkedin_url."""
    from api.services.person_entity import get_person_entity_store

    store = get_person_entity_store()
    people = store.get_all()

    with_linkedin = [p for p in people if p.linkedin_url]
    with_linkedin.sort(key=lambda p: p.relationship_strength or 0, reverse=True)

    return with_linkedin[:limit]


def load_state() -> dict:
    """Load or initialize scrape state."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_profiles": 0,
        "completed": [],
        "failed": {},
        "pending": [],
    }


def save_state(state: dict):
    """Save scrape state."""
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def scrape_linkedin_profiles(
    limit: int = 300,
    dry_run: bool = True,
    delay_min: int = 8,
    delay_max: int = 15,
    memory_threshold: float = 90.0,
    start_threshold: float = 75.0,
) -> dict:
    """
    Scrape LinkedIn profiles.

    Args:
        limit: Number of profiles to scrape
        dry_run: If True, just show what would be scraped
        delay_min: Minimum delay between profiles (seconds)
        delay_max: Maximum delay between profiles (seconds)
        memory_threshold: Stop mid-run if memory exceeds this % (default 90)
        start_threshold: Refuse to start if memory already exceeds this % (default 75)

    Returns:
        Stats dict
    """
    stats = {
        "profiles_to_scrape": 0,
        "already_completed": 0,
        "newly_completed": 0,
        "failed": 0,
        "skipped": 0,
        "stopped_reason": None,
    }

    # Configure memory monitor threshold
    memory_monitor.critical_threshold = memory_threshold

    # Check if memory is already too high to start
    mem_status = memory_monitor.check()
    if mem_status.percent_used >= start_threshold:
        reason = f"Memory already at {mem_status.percent_used:.1f}% (start threshold: {start_threshold}%). Free up memory before running."
        logger.error(f"Refusing to start: {reason}")
        stats["stopped_reason"] = reason
        return stats

    # Also check critical threshold
    if mem_status.should_stop:
        logger.error(f"Cannot start: {mem_status.reason}")
        stats["stopped_reason"] = mem_status.reason
        return stats

    logger.info(f"Starting with {mem_status.percent_used:.1f}% memory used, {mem_status.available_gb:.1f}GB available")

    # Get people to scrape
    people = get_top_people_with_linkedin(limit)
    stats["profiles_to_scrape"] = len(people)

    logger.info(f"Found {len(people)} people with LinkedIn URLs (top {limit} by strength)")

    if dry_run:
        logger.info("\nDRY RUN - would scrape these profiles:")
        for i, p in enumerate(people[:10]):
            logger.info(f"  {i+1}. {p.canonical_name} ({p.relationship_strength:.1f}) - {p.linkedin_url}")
        if len(people) > 10:
            logger.info(f"  ... and {len(people) - 10} more")
        return stats

    # Load state
    state = load_state()
    completed_ids = set(state.get("completed", []))

    # Initialize pending list if empty
    if not state.get("pending"):
        state["pending"] = [p.id for p in people if p.id not in completed_ids]
        state["total_profiles"] = len(people)
        save_state(state)

    # Count already completed
    stats["already_completed"] = len([p for p in people if p.id in completed_ids])
    logger.info(f"Already completed: {stats['already_completed']} profiles")

    # Scrape remaining profiles
    pending = [p for p in people if p.id not in completed_ids]
    logger.info(f"Remaining to scrape: {len(pending)} profiles")

    for i, person in enumerate(pending):
        # Memory check before each profile
        mem_status = memory_monitor.check()
        if mem_status.should_stop:
            logger.warning(f"Stopping gracefully: {mem_status.reason}")
            stats["stopped_reason"] = mem_status.reason
            save_state(state)
            break

        logger.info(f"\n[{i+1}/{len(pending)}] Scraping: {person.canonical_name}")
        logger.info(f"  URL: {person.linkedin_url}")

        try:
            # This is where we'd call Claude in Chrome MCP
            # For now, just mark as needing external scraping
            logger.info(f"  → Ready for MCP scraping (person_id: {person.id})")

            # The actual scraping will be done by calling this script
            # with special flags or by orchestrating from outside

        except Exception as e:
            logger.error(f"  Error: {e}")
            state["failed"][person.id] = str(e)
            stats["failed"] += 1
            save_state(state)
            continue

        # Delay between profiles
        if i < len(pending) - 1:
            delay = random.uniform(delay_min, delay_max)
            logger.info(f"  Waiting {delay:.1f}s before next profile...")
            time.sleep(delay)

    logger.info(f"\n=== Scrape Summary ===")
    logger.info(f"Total profiles: {stats['profiles_to_scrape']}")
    logger.info(f"Already completed: {stats['already_completed']}")
    logger.info(f"Newly completed: {stats['newly_completed']}")
    logger.info(f"Failed: {stats['failed']}")
    if stats["stopped_reason"]:
        logger.info(f"Stopped early: {stats['stopped_reason']}")
    logger.info(memory_monitor.get_summary())

    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape LinkedIn profiles")
    parser.add_argument("--execute", action="store_true", help="Actually scrape")
    parser.add_argument("--limit", type=int, default=300, help="Number of profiles")
    parser.add_argument("--delay-min", type=int, default=8, help="Min delay (seconds)")
    parser.add_argument("--delay-max", type=int, default=15, help="Max delay (seconds)")
    parser.add_argument("--memory-threshold", type=float, default=90.0,
                        help="Stop mid-run if memory exceeds this %% (default: 90)")
    parser.add_argument("--start-threshold", type=float, default=75.0,
                        help="Refuse to start if memory already exceeds this %% (default: 75)")

    args = parser.parse_args()

    scrape_linkedin_profiles(
        limit=args.limit,
        dry_run=not args.execute,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        memory_threshold=args.memory_threshold,
        start_threshold=args.start_threshold,
    )
