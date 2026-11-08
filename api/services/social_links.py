"""Parsing, matching keys, and list reconciliation for contact details.

A social link is a dict stored in ``PersonEntity.social_links``::

    {"platform", "value", "url", "handle", "match_key", "label", "source"}

``value`` is what was entered, ``url`` the canonical profile URL (or None),
and ``match_key`` the normalized account identity that makes two different
spellings of one profile compare equal. Labeled email and phone entries are
``{"value", "label"}`` dicts.
"""
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import parse_qs, quote, unquote, urlsplit

from config.social_platforms import PLATFORMS_BY_KEY, SOCIAL_PLATFORMS, ProfileRule, SocialPlatform

MAX_LABEL_LENGTH = 64
LINKEDIN = "linkedin"
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class InvalidContactDetail(ValueError):
    """A label, email, phone, or social value cannot be stored."""


@dataclass(frozen=True)
class ParsedAccount:
    """A social value resolved to one account on a platform."""

    url: Optional[str]
    handle: str
    match_key: str


def normalize_label(label: Optional[str]) -> Optional[str]:
    """Return a free-text label with collapsed whitespace, or None when blank."""
    if label is None:
        return None
    cleaned = " ".join(str(label).split())
    if not cleaned:
        return None
    if len(cleaned) > MAX_LABEL_LENGTH:
        raise InvalidContactDetail(f"Label must be {MAX_LABEL_LENGTH} characters or fewer")
    if not cleaned.isprintable():
        raise InvalidContactDetail("Label contains unsupported characters")
    return cleaned


def normalize_email(value: str) -> str:
    """Return a lowercased email address, or raise when it is not one."""
    cleaned = (value or "").strip().lower()
    if not _EMAIL_RE.fullmatch(cleaned):
        raise InvalidContactDetail("Not a valid email address")
    return cleaned


def _clean_identifier(identifier: str) -> str:
    return unicodedata.normalize("NFC", unquote(identifier)).strip().rstrip("/")


def _account(
    platform: SocialPlatform,
    identifier: str,
    kind: str,
    preserve_case: bool,
    url_template: Optional[str],
) -> ParsedAccount:
    url = url_template.format(id=quote(identifier, safe="@/")) if url_template else None
    comparable = identifier if preserve_case else identifier.lower()
    return ParsedAccount(url=url, handle=identifier, match_key=f"{kind}:{comparable}" if kind else comparable)


def _host_matches(platform: SocialPlatform, host: str) -> bool:
    return any(host == h or host.endswith(f".{h}") for h in platform.hosts)


def _unwrap_doubled_url(raw: str) -> str:
    """Keep the innermost URL of ``site.com/https://site.com/user`` values."""
    lowered = raw.lower()
    start = max(lowered.rfind("https://"), lowered.rfind("http://"))
    return raw[start:] if start > 0 else raw


def _apply_rule(platform: SocialPlatform, rule: ProfileRule, path: str, query: str) -> Optional[ParsedAccount]:
    match = re.match(rule.pattern, path, re.IGNORECASE)
    if not match:
        return None
    if rule.query_param:
        values = parse_qs(query).get(rule.query_param)
        if not values:
            return None
        identifier = values[0]
    else:
        identifier = match.group(1)
    identifier = _clean_identifier(identifier)
    if not identifier or identifier.lower() in platform.reserved_paths:
        return None
    return _account(
        platform,
        identifier,
        rule.kind,
        rule.preserve_case,
        rule.url_template or platform.url_template,
    )


def _parse_url(platform: SocialPlatform, raw: str) -> Optional[ParsedAccount]:
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() not in ("http", "https") or not _host_matches(platform, host):
        return None
    for rule in platform.rules:
        account = _apply_rule(platform, rule, parts.path, parts.query)
        if account:
            return account
    return None


def _looks_like_schemeless_url(platform: SocialPlatform, raw: str) -> bool:
    host = raw.split("/", 1)[0].lower()
    return _host_matches(platform, host)


def parse_social_value(platform_key: str, value: str) -> ParsedAccount:
    """Resolve a URL or handle to an account, raising when it is not one."""
    platform = PLATFORMS_BY_KEY.get(platform_key)
    if platform is None:
        raise InvalidContactDetail(f"Unknown platform '{platform_key}'")
    raw = _unwrap_doubled_url((value or "").strip())
    if not raw:
        raise InvalidContactDetail("A URL or handle is required")

    if "://" in raw or _looks_like_schemeless_url(platform, raw):
        account = _parse_url(platform, raw if "://" in raw else f"https://{raw}")
        if account is None:
            raise InvalidContactDetail(f"Not a {platform.label} profile URL")
        return account

    handle = unicodedata.normalize("NFC", raw.lstrip("@"))
    if platform.id_pattern and re.fullmatch(platform.id_pattern, handle):
        return _account(platform, handle, "id", False, platform.id_url_template)
    if not re.fullmatch(platform.handle_pattern, handle):
        raise InvalidContactDetail(f"Not a valid {platform.label} handle")
    return _account(platform, handle, "", False, platform.url_template)


def find_account_for_url(value: str) -> Optional[tuple[str, ParsedAccount]]:
    """Identify which registered platform a profile URL belongs to."""
    raw = _unwrap_doubled_url((value or "").strip())
    for platform in SOCIAL_PLATFORMS:
        if "://" not in raw and not _looks_like_schemeless_url(platform, raw):
            continue
        try:
            return platform.key, parse_social_value(platform.key, raw)
        except InvalidContactDetail:
            continue
    return None


def build_social_link(
    platform: str,
    value: str,
    label: Optional[str] = None,
    source: str = "manual",
) -> dict:
    """Build a validated link, raising InvalidContactDetail on a bad value."""
    account = parse_social_value(platform, value)
    return {
        "platform": platform,
        "value": value.strip(),
        "url": account.url,
        "handle": account.handle,
        "match_key": account.match_key,
        "label": normalize_label(label),
        "source": source,
    }


def build_preserved_link(platform: str, value: str, source: str) -> dict:
    """Build a link for an existing stored value, keeping unparseable values."""
    try:
        return build_social_link(platform, value, source=source)
    except InvalidContactDetail:
        raw = value.strip()
        return {
            "platform": platform,
            "value": raw,
            "url": raw if raw.lower().startswith(("http://", "https://")) else None,
            "handle": raw,
            "match_key": None,
            "label": None,
            "source": source,
        }


def social_identity(link: dict) -> str:
    """Return the string that identifies one account across spellings."""
    return f"{link['platform']}:{link.get('match_key') or link['value']}"


def social_link_id(link: dict) -> str:
    """Return the id addressing a link without placing it in a URL path."""
    return hashlib.sha256(social_identity(link).encode()).hexdigest()[:16]


def contact_entry_id(kind: str, value: str) -> str:
    """Return the id addressing an email or phone entry."""
    return hashlib.sha256(f"{kind}:{value.lower()}".encode()).hexdigest()[:16]


def linkedin_field_value(link: dict) -> str:
    """Return the ``linkedin_url`` representation of a LinkedIn link."""
    value = link["value"]
    if value.lower().startswith(("http://", "https://")):
        return value
    return link.get("url") or value


def reconcile_entries(
    values: list[str],
    entries: list[dict],
    key: Callable[[str], str] = str,
) -> list[dict]:
    """Return entries matching ``values`` exactly, carrying labels across."""
    labels = {key(e["value"]): e.get("label") for e in entries or []}
    seen: set[str] = set()
    reconciled = []
    for value in values or []:
        if not value or key(value) in seen:
            continue
        seen.add(key(value))
        reconciled.append({"value": value, "label": labels.get(key(value))})
    return reconciled


def merge_entries(target: list[dict], incoming: list[dict], key: Callable[[str], str] = str) -> list[dict]:
    """Fill unlabeled target entries from labels on the same values."""
    labels = {key(e["value"]): e.get("label") for e in incoming or [] if e.get("label")}
    return [
        {**entry, "label": entry.get("label") or labels.get(key(entry["value"]))}
        for entry in target
    ]


def merge_social_links(target: list[dict], incoming: list[dict]) -> list[dict]:
    """Union links by account, filling a missing label from the duplicate."""
    merged = [dict(link) for link in target]
    by_identity = {social_identity(link): link for link in merged}
    for link in incoming or []:
        existing = by_identity.get(social_identity(link))
        if existing is None:
            copied = dict(link)
            merged.append(copied)
            by_identity[social_identity(copied)] = copied
        elif not existing.get("label") and link.get("label"):
            existing["label"] = link["label"]
    return merged


def reconcile_linkedin(
    links: list[dict],
    linkedin_url: Optional[str],
    dismissed: list[str],
    *,
    empty_means_cleared: bool,
) -> tuple[list[dict], Optional[str]]:
    """Keep ``linkedin_url`` and the first LinkedIn link pointing at one account.

    Sync jobs and repair scripts assign ``linkedin_url`` directly, so a new
    value is added as the primary link. ``empty_means_cleared`` is True when
    persisting: an empty field then removes the primary link, whereas on
    construction it is filled in from the links.
    """
    links = list(links or [])
    indexes = [i for i, link in enumerate(links) if link["platform"] == LINKEDIN]

    if not linkedin_url:
        if not indexes:
            return links, linkedin_url
        if empty_means_cleared:
            links.pop(indexes[0])
            remaining = [link for link in links if link["platform"] == LINKEDIN]
            return links, linkedin_field_value(remaining[0]) if remaining else None
        return links, linkedin_field_value(links[indexes[0]])

    assigned = build_preserved_link(LINKEDIN, linkedin_url, "linkedin_url")
    identity = social_identity(assigned)
    if identity in (dismissed or []):
        remaining = [link for link in links if link["platform"] == LINKEDIN]
        return links, linkedin_field_value(remaining[0]) if remaining else None

    found = next((i for i in indexes if social_identity(links[i]) == identity), None)
    if found is None:
        links.insert(indexes[0] if indexes else len(links), assigned)
    elif found != indexes[0]:
        links.insert(indexes[0], links.pop(found))
    return links, linkedin_url


def platform_catalog() -> list[dict]:
    """Return the registry fields clients need to build forms."""
    return [
        {"key": p.key, "label": p.label, "icon": p.icon, "placeholder": p.placeholder}
        for p in SOCIAL_PLATFORMS
    ]
