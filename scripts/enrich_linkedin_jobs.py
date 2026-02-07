#!/usr/bin/env python3
"""
LinkedIn Job Enrichment - Add Industry and Seniority Classifications

This script adds industry and seniority classifications to jobs in the
LinkedIn extracted data. It uses pattern matching on company names and
job titles to classify each role.

IMPORTANT: As of Feb 2026, industry/seniority classification is done
by Claude during the scraping process, making this script largely obsolete.
It remains as a reference for the canonical industry list and classification
patterns.

Industry Categories:
  Tech, Non-profit, Politics, Government, Finance, Healthcare, Media,
  Consulting, Education, Logistics, Real Estate, Military, Entertainment,
  Retail, Energy, Legal, Religious, Other

Seniority Levels:
  Executive, Manager/Mid-level, Junior

See docs/architecture/DATA-AND-SYNC.md for full documentation.

Usage:
    python scripts/enrich_linkedin_jobs.py              # Dry run
    python scripts/enrich_linkedin_jobs.py --execute    # Actually modify the file
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import json
import re

DATA_FILE = Path(__file__).parent.parent / "data" / "linkedin_extracted.json"
INDUSTRY_MAPPINGS_FILE = Path(__file__).parent.parent / "config" / "linkedin_industry_mappings.json"

# Industry classifications
INDUSTRIES = [
    "Tech",
    "Non-profit",
    "Politics",
    "Government",
    "Finance",
    "Healthcare",
    "Media",
    "Consulting",
    "Education",
    "Logistics",
    "Real Estate",
    "Military",
    "Entertainment",
    "Retail",
    "Energy",
    "Legal",
    "Religious",
    "Other"
]

def load_company_industry_map() -> dict:
    """Load company -> industry mappings from config file."""
    if INDUSTRY_MAPPINGS_FILE.exists():
        with open(INDUSTRY_MAPPINGS_FILE) as f:
            data = json.load(f)
            # Remove _comment key if present
            return {k: v for k, v in data.items() if not k.startswith("_")}
    return {}

# Known company -> industry mappings (loaded from config/linkedin_industry_mappings.json)
COMPANY_INDUSTRY_MAP = load_company_industry_map()

# Seniority patterns
EXECUTIVE_PATTERNS = [
    r'\b(ceo|coo|cfo|cto|cmo|cio)\b',
    r'\bchief\s+\w+\s+officer\b',
    r'\b(president|vice\s+president|vp)\b',
    r'\b(founder|co-founder|cofounder)\b',
    r'\bexecutive\s+director\b',
    r'\bmanaging\s+director\b',
    r'\b(partner|principal)\b',
    r'\bboard\s+member\b',
    r'\bgeneral\s+manager\b',
    r'\bsvp\b',
    r'\bevp\b',
    r'\bsenior\s+vice\s+president\b',
]

MANAGER_PATTERNS = [
    r'\bdirector\b(?!\s+of\s+(volunteer|intern))',
    r'\bhead\s+of\b',
    r'\bmanager\b',
    r'\bsenior\b',
    r'\blead\b',
    r'\bprogram\s+director\b',
    r'\bcampaign\s+manager\b',
    r'\bteam\s+lead\b',
    r'\bsupervisor\b',
]

JUNIOR_PATTERNS = [
    r'\bintern\b',
    r'\bassistant\b',
    r'\bassociate\b(?!\s+director)',
    r'\bcoordinator\b',
    r'\bspecialist\b',
    r'\banalyst\b',
    r'\bjunior\b',
    r'\bentry\s+level\b',
]


def classify_seniority(title: str) -> str:
    """Classify job title into seniority level."""
    if not title:
        return "Unknown"

    title_lower = title.lower()

    # Check executive patterns first (highest priority)
    for pattern in EXECUTIVE_PATTERNS:
        if re.search(pattern, title_lower):
            return "Executive"

    # Check junior patterns next
    for pattern in JUNIOR_PATTERNS:
        if re.search(pattern, title_lower):
            return "Junior"

    # Check manager patterns
    for pattern in MANAGER_PATTERNS:
        if re.search(pattern, title_lower):
            return "Manager/Mid-level"

    # Default based on common patterns
    if any(word in title_lower for word in ['engineer', 'developer', 'designer', 'consultant', 'volunteer']):
        return "Junior"

    return "Manager/Mid-level"  # Default assumption


def classify_industry(company: str, title: str = None, description: str = None) -> str:
    """Classify company into industry."""
    if not company:
        return "Other"

    company_lower = company.lower().strip()

    # Check direct mappings
    for known_company, industry in COMPANY_INDUSTRY_MAP.items():
        if known_company in company_lower or company_lower in known_company:
            return industry

    # Keyword-based inference
    company_and_context = f"{company_lower} {(title or '').lower()} {(description or '').lower()}"

    # Politics indicators
    if any(word in company_and_context for word in ['campaign', 'for senate', 'for congress', 'for mayor', 'for president', 'democratic', 'republican', 'voter', 'political', 'pac']):
        return "Politics"

    # Non-profit indicators
    if any(word in company_and_context for word in ['foundation', 'nonprofit', 'non-profit', 'charity', 'ngo', 'association', '501c']):
        return "Non-profit"

    # Tech indicators
    if any(word in company_and_context for word in ['software', 'tech', 'ai', 'data', 'saas', 'platform', 'app', 'digital']):
        return "Tech"

    # Healthcare indicators
    if any(word in company_and_context for word in ['health', 'medical', 'hospital', 'clinic', 'pharma', 'biotech']):
        return "Healthcare"

    # Finance indicators
    if any(word in company_and_context for word in ['bank', 'investment', 'capital', 'fund', 'financial', 'venture']):
        return "Finance"

    # Government indicators
    if any(word in company_and_context for word in ['government', 'federal', 'state', 'city of', 'department of', 'agency']):
        return "Government"

    # Military indicators
    if any(word in company_and_context for word in ['army', 'navy', 'air force', 'marine', 'military', 'defense']):
        return "Military"

    # Education indicators
    if any(word in company_and_context for word in ['university', 'college', 'school', 'academy', 'institute']):
        return "Education"

    # Consulting indicators
    if any(word in company_and_context for word in ['consulting', 'consultant', 'advisory', 'advisors']):
        return "Consulting"

    # Self-employed / freelance
    if any(word in company_lower for word in ['self employed', 'self-employed', 'freelance', 'independent', 'free agent']):
        return "Consulting"

    # Career transitions
    if 'career' in company_lower and ('break' in company_lower or 'transition' in company_lower):
        return "Other"

    return "Unknown"


def enrich_jobs(data: dict) -> tuple[dict, dict]:
    """Enrich all jobs with industry and seniority. Returns (enriched_data, stats)."""
    stats = {
        "profiles_processed": 0,
        "jobs_enriched": 0,
        "industries": {},
        "seniorities": {},
        "unknown_industries": [],
    }

    for profile in data.get("profiles", []):
        stats["profiles_processed"] += 1

        for job in profile.get("experience", []):
            company = job.get("company", "")
            title = job.get("title", "")
            description = job.get("description", "")

            # Classify
            industry = classify_industry(company, title, description)
            seniority = classify_seniority(title)

            # Add to job
            job["industry"] = industry
            job["seniority"] = seniority

            # Track stats
            stats["jobs_enriched"] += 1
            stats["industries"][industry] = stats["industries"].get(industry, 0) + 1
            stats["seniorities"][seniority] = stats["seniorities"].get(seniority, 0) + 1

            if industry == "Unknown":
                stats["unknown_industries"].append({
                    "person": profile.get("name"),
                    "company": company,
                    "title": title
                })

    return data, stats


def main():
    parser = argparse.ArgumentParser(description="Enrich LinkedIn jobs with industry and seniority")
    parser.add_argument("--execute", action="store_true", help="Actually modify the file")
    args = parser.parse_args()

    # Load data
    with open(DATA_FILE) as f:
        data = json.load(f)

    print(f"Loaded {len(data.get('profiles', []))} profiles")

    # Enrich
    enriched_data, stats = enrich_jobs(data)

    # Print stats
    print(f"\n=== Enrichment Stats ===")
    print(f"Profiles processed: {stats['profiles_processed']}")
    print(f"Jobs enriched: {stats['jobs_enriched']}")

    print(f"\nIndustry breakdown:")
    for industry, count in sorted(stats["industries"].items(), key=lambda x: -x[1]):
        print(f"  {industry}: {count}")

    print(f"\nSeniority breakdown:")
    for seniority, count in sorted(stats["seniorities"].items(), key=lambda x: -x[1]):
        print(f"  {seniority}: {count}")

    if stats["unknown_industries"]:
        print(f"\nUnknown industries ({len(stats['unknown_industries'])}):")
        for item in stats["unknown_industries"][:10]:
            print(f"  {item['person']}: {item['company']} - {item['title']}")
        if len(stats["unknown_industries"]) > 10:
            print(f"  ... and {len(stats['unknown_industries']) - 10} more")

    if args.execute:
        with open(DATA_FILE, "w") as f:
            json.dump(enriched_data, f, indent=2)
        print(f"\nSaved enriched data to {DATA_FILE}")
    else:
        print(f"\nDRY RUN - use --execute to save changes")


if __name__ == "__main__":
    main()
