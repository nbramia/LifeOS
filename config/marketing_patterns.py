"""
Marketing Email Patterns and Blocklist Configuration.

Centralized configuration for identifying marketing/automated emails and domains
that should be excluded from person entity creation and linking.

Used by:
- scripts/sync_gmail_calendar_interactions.py (email filtering)
- scripts/link_source_entities.py (domain blocklist for entity linking)
"""

# =============================================================================
# EMAIL ADDRESS PREFIX PATTERNS
# =============================================================================
# Email address prefixes that typically indicate automated/marketing emails.
# These are checked against the local part of the email (before @).

MARKETING_EMAIL_PREFIXES = {
    # Standard no-reply patterns
    'noreply', 'no-reply', 'no_reply', 'donotreply', 'do-not-reply', 'do_not_reply',

    # Newsletter/marketing
    'newsletter', 'newsletters', 'news',
    'marketing', 'promo', 'promotions', 'offers', 'deals',

    # Notifications
    'notifications', 'notification', 'notify', 'alert', 'alerts',
    'updates', 'update', 'info', 'information',

    # Support/service
    'support', 'help', 'helpdesk', 'customerservice', 'customer-service',

    # System/automated
    'mailer', 'mailer-daemon', 'postmaster', 'webmaster',
    'bounce', 'bounces', 'unsubscribe',
    'admin', 'administrator', 'system', 'automated',
    'reply', 'replies',

    # Transactional
    'billing', 'invoices', 'invoice', 'receipts', 'receipt',
    'orders', 'order', 'shipping', 'delivery',
    'feedback', 'survey', 'surveys',

    # Generic
    'hello', 'hi', 'team', 'contact',

    # Newsletter-specific prefixes
    'digest', 'daily', 'weekly', 'monthly', 'morning', 'evening',
    'playbook', 'briefing', 'roundup', 'recap', 'summary',
    'forecast', 'report', 'insider', 'dispatch', 'bulletin',
    'edition', 'highlights', 'headlines', 'breaking',
}

# =============================================================================
# SENDER NAME PATTERNS
# =============================================================================
# Patterns checked against sender display names that indicate marketing/newsletters.

MARKETING_NAME_PATTERNS = {
    'newsletter', 'digest', 'playbook', 'briefing', 'roundup',
    'weekly', 'daily', 'morning', 'evening', 'update',
    'forecast', 'report', 'insider', 'dispatch', 'bulletin',
    'alerts', 'notifications', 'noreply', 'no-reply',
}

# =============================================================================
# COMMERCIAL SENDER SUBSTRINGS
# =============================================================================
# Companies that only send automated/transactional emails.
# Matched as substring in email or sender name.

COMMERCIAL_SENDER_SUBSTRINGS = {
    'amazon', 'ebay', 'etsy', 'gusto', 'gustin',
    'capitalone', 'capital one', 'monarch', 'usaa',
}

# =============================================================================
# MARKETING DOMAINS - DEDICATED SENDING DOMAINS
# =============================================================================
# Domains that are EXCLUSIVELY used for automated/marketing emails.
# NOTE: Do NOT include domains where real people work (google.com, amazon.com, etc.)
# Only include domains that are dedicated sending infrastructure.

MARKETING_DOMAINS = {
    # === Email Service Providers (ESP) sending domains ===
    'mailchimp.com', 'mail.mailchimp.com', 'mailchimpapp.net', 'mailchi.mp',
    'sendgrid.net',  # Note: sendgrid.com is their corporate domain
    'ccsend.com',  # Constant Contact sending domain
    'mailgun.org',
    'amazonses.com',  # AWS SES (not amazon.com where employees work)
    'postmarkapp.com',
    'sparkpostmail.com',
    'mandrillapp.com',
    'sendinblue.com', 'brevo.com',
    'klaviyomail.com',  # Not klaviyo.com
    'hubspotmail.com', 'hs-mail.com',  # Not hubspot.com
    'intercom-mail.com',  # Not intercom.io
    'responsys.net',
    'sailthru.com',
    'exacttarget.com', 'sfmc.co',  # Salesforce Marketing Cloud
    'mcsv.net',  # Mailchimp CDN
    'list-manage.com',  # Mailchimp tracking

    # Campaign Monitor sending domains (cmail1-50)
    'cmail1.com', 'cmail2.com', 'cmail3.com', 'cmail4.com', 'cmail5.com',
    'cmail6.com', 'cmail7.com', 'cmail8.com', 'cmail9.com', 'cmail10.com',
    'cmail11.com', 'cmail12.com', 'cmail13.com', 'cmail14.com', 'cmail15.com',
    'cmail16.com', 'cmail17.com', 'cmail18.com', 'cmail19.com', 'cmail20.com',
    'createsend.com', 'createsend1.com', 'createsend2.com',

    # === Social Media Notification Domains ===
    'facebookmail.com',  # Not fb.com/meta.com where employees work
    'linkedin-email.com',  # Not linkedin.com
    'redditmail.com',  # Not reddit.com
    'twittermail.com',  # Not twitter.com/x.com
    'instagrammail.com',
    'pinterest.info',

    # === Service Notification Domains ===
    'shopifyemail.com',  # Not shopify.com
    'dropboxmail.com',  # Not dropbox.com
    'slack-msgs.com',  # Not slack.com
    'githubemail.com', 'github.net',  # Not github.com
    'zoom.us.email',
    'stripe.network',  # Not stripe.com
    'squareup.email',

    # === Newsletter Platforms ===
    'substack.com',  # Newsletter platform (authors use @substack.com)
    'substackcdn.com',
    'mail.beehiiv.com',  # Newsletter platform
    'ghost.io',  # Newsletter platform
    'buttondown.email',
    'convertkit.com', 'ck.page',
    'revue.email',

    # === News Organization Email Domains ===
    # These are dedicated sending domains for newsletters, not editorial contact
    'email.politico.com', 'politico.email',
    'email.axios.com',
    'email.theatlantic.com',
    'email.nytimes.com', 'e.newyorktimes.com', 'nytmail.com',
    'email.washingtonpost.com', 'washpost.com',
    'email.wsj.com', 'wsj.email',
    'e.forbes.com', 'forbes.email',
    'email.bloomberg.com',
    'email.cnbc.com',
    'email.cnn.com',
    'email.bbc.com', 'bbcemail.com',
    'email.theguardian.com',
    'email.reuters.com',
    'news.ycombinator.com',  # Hacker News
    'email.techcrunch.com',
    'email.theverge.com',
    'email.wired.com',
    'email.arstechnica.com',
    'email.morningbrew.com',
    'email.themorningbrew.com',
    'email.theskimm.com',

    # === E-commerce Notification Domains ===
    'amazonses.com',
    'email.ebay.com',
    'em.ebay.com',
    'email.etsy.com',
    'shopify.email',
    'email.airbnb.com',
    'email.uber.com',
    'email.lyft.com',
    'email.doordash.com',

    # === Financial Services Notification Domains ===
    'email.chase.com',
    'email.bankofamerica.com',
    'email.wellsfargo.com',
    'email.capitalone.com',
    'email.amex.com',
    'email.paypal.com',
    'email.venmo.com',
    'email.robinhood.com',
    'email.coinbase.com',

    # === Tech Platform Notification Domains ===
    'email.apple.com', 'apple.news',
    'email.google.com', 'googlemail.com',  # Not gmail.com
    'email.microsoft.com',
    'email.spotify.com',
    'email.netflix.com',
    'email.hulu.com',
    'email.disney.com',
}

# =============================================================================
# BLOCKLISTED DOMAINS FOR SOURCE ENTITY LINKING
# =============================================================================
# Domains that should NEVER create person entities or be linked.
# Source entities from these domains are kept but never linked.
# This is a superset of MARKETING_DOMAINS plus additional domains.

BLOCKLISTED_DOMAINS = MARKETING_DOMAINS | {
    # Additional domains that shouldn't create people

    # === Automated Reply Domains ===
    'mailer-daemon.googlemail.com',
    'mailer-daemon.google.com',

    # === Transaction/Receipt Domains ===
    'receipts.amazon.com',
    'auto-confirm.amazon.com',
    'order-update.amazon.com',
    'ship-confirm.amazon.com',
    'returns.amazon.com',

    # === Generic Bounce/Error Domains ===
    'bounce.mailgun.org',
    'bounce.sendgrid.net',
}


def get_domain_from_email(email: str) -> str | None:
    """Extract domain from email address."""
    if not email or '@' not in email:
        return None
    return email.lower().split('@')[1]


def is_blocklisted_domain(email: str) -> bool:
    """
    Check if an email's domain is in the blocklist.

    Checks both the full domain and parent domains.
    E.g., for "email.nytimes.com", checks both "email.nytimes.com" and "nytimes.com"

    Args:
        email: Email address to check

    Returns:
        True if domain is blocklisted
    """
    domain = get_domain_from_email(email)
    if not domain:
        return False

    # Check full domain and all parent domains
    domain_parts = domain.split('.')
    for i in range(len(domain_parts) - 1):
        check_domain = '.'.join(domain_parts[i:])
        if check_domain in BLOCKLISTED_DOMAINS:
            return True

    return False
