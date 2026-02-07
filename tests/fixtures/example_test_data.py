"""
Example test data structure - copy to production_test_data.py and customize.

This file provides a template for production test data. Copy it to
production_test_data.py and fill in your real values. The production
file is gitignored and will be used by tests when available.
"""

# Your work domain (matches LIFEOS_WORK_DOMAIN in .env)
WORK_DOMAIN = "yourcompany.com"

# Test person using your real work email format
TEST_WORK_CONTACT = {
    "name": "Test Colleague",
    "email": f"colleague@{WORK_DOMAIN}",
    "company": "Your Company",
}

# Real colleague names for entity resolution tests
COLLEAGUE_NAMES = [
    "First Colleague",
    "Second Colleague",
]

# Personal contact for testing personal vs work routing
TEST_PERSONAL_CONTACT = {
    "name": "Test Friend",
    "email": "friend@example.com",
    "phone": "+15551234567",
}

# Known family member for category tests
TEST_FAMILY_CONTACT = {
    "name": "Family Member",
    "email": "family@example.com",
    "category": "family",
}
