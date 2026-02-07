"""
Apple Contacts integration for LifeOS CRM.

Reads contacts from macOS AddressBook/Contacts.app using the Contacts framework.
Creates SourceEntity records for each contact.

Requires:
- macOS
- pyobjc-framework-Contacts
- Contacts permission granted to Python/Terminal
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from api.services.source_entity import SourceEntity, SourceEntityStore

logger = logging.getLogger(__name__)

# Source type constant
SOURCE_CONTACTS = "contacts"

# Check if Contacts framework is available (macOS only)
_CONTACTS_AVAILABLE = False
try:
    import Contacts  # noqa: F401
    _CONTACTS_AVAILABLE = True
except ImportError:
    logger.warning("pyobjc-framework-Contacts not available. Apple Contacts integration disabled.")


@dataclass
class AppleContact:
    """Represents an Apple Contacts entry."""
    identifier: str
    given_name: str = ""
    family_name: str = ""
    full_name: str = ""
    nickname: str = ""
    organization: str = ""
    job_title: str = ""
    department: str = ""
    emails: list[dict] = field(default_factory=list)  # [{"label": str, "value": str}]
    phones: list[dict] = field(default_factory=list)  # [{"label": str, "value": str}]
    addresses: list[dict] = field(default_factory=list)
    social_profiles: list[dict] = field(default_factory=list)
    note: str = ""
    image_available: bool = False
    birthday: Optional[datetime] = None

    @property
    def display_name(self) -> str:
        """Get display name with fallbacks."""
        if self.full_name:
            return self.full_name
        parts = [self.given_name, self.family_name]
        name = " ".join(p for p in parts if p)
        if name:
            return name
        if self.organization:
            return self.organization
        if self.emails:
            return self.emails[0]["value"]
        return self.identifier

    @property
    def primary_email(self) -> Optional[str]:
        """Get primary email address."""
        if self.emails:
            return self.emails[0]["value"]
        return None

    @property
    def primary_phone(self) -> Optional[str]:
        """Get primary phone number."""
        if self.phones:
            return self.phones[0]["value"]
        return None

    def to_dict(self) -> dict:
        """Convert to dict for API response."""
        return {
            "identifier": self.identifier,
            "given_name": self.given_name,
            "family_name": self.family_name,
            "full_name": self.full_name,
            "display_name": self.display_name,
            "nickname": self.nickname,
            "organization": self.organization,
            "job_title": self.job_title,
            "department": self.department,
            "emails": self.emails,
            "phones": self.phones,
            "addresses": self.addresses,
            "social_profiles": self.social_profiles,
            "note": self.note,
            "image_available": self.image_available,
            "birthday": self.birthday.isoformat() if self.birthday else None,
        }


class AppleContactsReader:
    """
    Reader for Apple Contacts using the Contacts framework.

    Requires:
    - macOS
    - pyobjc-framework-Contacts
    - Terminal/Python must have Contacts permission
    """

    def __init__(self):
        self._store = None
        self._available = _CONTACTS_AVAILABLE

    @property
    def is_available(self) -> bool:
        """Check if Contacts framework is available."""
        return self._available

    def _get_store(self):
        """Get or create CNContactStore."""
        if not self._available:
            raise RuntimeError("Apple Contacts not available on this platform")

        if self._store is None:
            import Contacts
            self._store = Contacts.CNContactStore.alloc().init()
        return self._store

    def check_authorization(self) -> str:
        """
        Check Contacts authorization status.

        Returns: "authorized", "denied", "restricted", or "not_determined"
        """
        if not self._available:
            return "not_available"

        import Contacts

        status = Contacts.CNContactStore.authorizationStatusForEntityType_(
            Contacts.CNEntityTypeContacts
        )

        status_map = {
            Contacts.CNAuthorizationStatusAuthorized: "authorized",
            Contacts.CNAuthorizationStatusDenied: "denied",
            Contacts.CNAuthorizationStatusRestricted: "restricted",
            Contacts.CNAuthorizationStatusNotDetermined: "not_determined",
        }
        return status_map.get(status, "unknown")

    def request_access(self) -> bool:
        """
        Request access to Contacts.

        Note: This is blocking and shows a system dialog.
        Returns True if access was granted.
        """
        if not self._available:
            return False

        import Contacts
        from Foundation import NSRunLoop, NSDate

        store = self._get_store()
        result = {"granted": False, "done": False}

        def completion(granted, error):
            result["granted"] = granted
            result["done"] = True
            if error:
                logger.error(f"Contacts access error: {error}")

        store.requestAccessForEntityType_completionHandler_(
            Contacts.CNEntityTypeContacts,
            completion,
        )

        # Wait for completion (blocking)
        while not result["done"]:
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.1)
            )

        return result["granted"]

    def _extract_labeled_values(self, labeled_values) -> list[dict]:
        """Extract label/value pairs from CNLabeledValue array."""
        results = []
        if labeled_values:
            for lv in labeled_values:
                label = str(lv.label()) if lv.label() else "other"
                value = lv.value()
                if hasattr(value, "stringValue"):
                    value = str(value.stringValue())
                elif hasattr(value, "__str__"):
                    value = str(value)
                results.append({"label": label, "value": value})
        return results

    def _extract_addresses(self, postal_addresses) -> list[dict]:
        """Extract postal addresses."""
        results = []
        if postal_addresses:
            for lv in postal_addresses:
                label = str(lv.label()) if lv.label() else "other"
                addr = lv.value()
                results.append({
                    "label": label,
                    "street": str(addr.street()) if addr.street() else "",
                    "city": str(addr.city()) if addr.city() else "",
                    "state": str(addr.state()) if addr.state() else "",
                    "postal_code": str(addr.postalCode()) if addr.postalCode() else "",
                    "country": str(addr.country()) if addr.country() else "",
                })
        return results

    def _extract_social_profiles(self, social_profiles) -> list[dict]:
        """Extract social profile information."""
        results = []
        if social_profiles:
            for lv in social_profiles:
                profile = lv.value()
                results.append({
                    "service": str(profile.service()) if profile.service() else "",
                    "username": str(profile.username()) if profile.username() else "",
                    "url": str(profile.urlString()) if profile.urlString() else "",
                })
        return results

    def get_contact_by_identifier(self, identifier: str) -> Optional[AppleContact]:
        """
        Fetch a single contact by its identifier (UUID).

        Args:
            identifier: Contact UUID (from CNContact.identifier)

        Returns:
            AppleContact if found, None otherwise
        """
        if not self._available:
            logger.warning("Apple Contacts not available")
            return None

        import Contacts

        store = self._get_store()

        # Keys to fetch
        keys_to_fetch = [
            Contacts.CNContactIdentifierKey,
            Contacts.CNContactGivenNameKey,
            Contacts.CNContactFamilyNameKey,
            Contacts.CNContactNicknameKey,
            Contacts.CNContactOrganizationNameKey,
            Contacts.CNContactJobTitleKey,
            Contacts.CNContactDepartmentNameKey,
            Contacts.CNContactEmailAddressesKey,
            Contacts.CNContactPhoneNumbersKey,
            Contacts.CNContactPostalAddressesKey,
            Contacts.CNContactSocialProfilesKey,
            Contacts.CNContactNoteKey,
            Contacts.CNContactImageDataAvailableKey,
            Contacts.CNContactBirthdayKey,
        ]

        try:
            contact = store.unifiedContactWithIdentifier_keysToFetch_error_(
                identifier, keys_to_fetch, None
            )
            if contact is None:
                return None

            # Extract birthday
            birthday = None
            if contact.birthday():
                bd = contact.birthday()
                try:
                    # NSDateComponentUndefined is a very large number; treat as "no year"
                    year = bd.year() if bd.year() and bd.year() < 9999 else 1900
                    birthday = datetime(
                        year=year,
                        month=bd.month(),
                        day=bd.day(),
                        tzinfo=timezone.utc,
                    )
                except (ValueError, AttributeError, OverflowError):
                    pass

            # Note access may fail due to macOS permissions
            try:
                note = str(contact.note()) if contact.note() else ""
            except Exception:
                note = ""

            return AppleContact(
                identifier=str(contact.identifier()),
                given_name=str(contact.givenName()) if contact.givenName() else "",
                family_name=str(contact.familyName()) if contact.familyName() else "",
                nickname=str(contact.nickname()) if contact.nickname() else "",
                organization=str(contact.organizationName()) if contact.organizationName() else "",
                job_title=str(contact.jobTitle()) if contact.jobTitle() else "",
                department=str(contact.departmentName()) if contact.departmentName() else "",
                emails=self._extract_labeled_values(contact.emailAddresses()),
                phones=self._extract_labeled_values(contact.phoneNumbers()),
                addresses=self._extract_addresses(contact.postalAddresses()),
                social_profiles=self._extract_social_profiles(contact.socialProfiles()),
                note=note,
                image_available=bool(contact.imageDataAvailable()),
                birthday=birthday,
            )
        except Exception as e:
            logger.debug(f"Contact not found or error: {identifier}: {e}")
            return None

    def get_all_contacts(self) -> list[AppleContact]:
        """
        Fetch all contacts from Apple Contacts.

        Returns list of AppleContact objects.
        """
        if not self._available:
            logger.warning("Apple Contacts not available")
            return []

        import Contacts

        store = self._get_store()

        # Keys to fetch
        keys_to_fetch = [
            Contacts.CNContactIdentifierKey,
            Contacts.CNContactGivenNameKey,
            Contacts.CNContactFamilyNameKey,
            Contacts.CNContactNicknameKey,
            Contacts.CNContactOrganizationNameKey,
            Contacts.CNContactJobTitleKey,
            Contacts.CNContactDepartmentNameKey,
            Contacts.CNContactEmailAddressesKey,
            Contacts.CNContactPhoneNumbersKey,
            Contacts.CNContactPostalAddressesKey,
            Contacts.CNContactSocialProfilesKey,
            Contacts.CNContactNoteKey,
            Contacts.CNContactImageDataAvailableKey,
            Contacts.CNContactBirthdayKey,
        ]

        # Create fetch request
        request = Contacts.CNContactFetchRequest.alloc().initWithKeysToFetch_(
            keys_to_fetch
        )

        contacts = []
        error = None

        def enumerate_contact(contact, stop):
            try:
                # Extract birthday
                birthday = None
                if contact.birthday():
                    bd = contact.birthday()
                    try:
                        # NSDateComponentUndefined is a very large number; treat as "no year"
                        year = bd.year() if bd.year() and bd.year() < 9999 else 1900
                        birthday = datetime(
                            year=year,
                            month=bd.month(),
                            day=bd.day(),
                            tzinfo=timezone.utc,
                        )
                    except (ValueError, AttributeError, OverflowError):
                        pass

                # Note access may fail due to macOS permissions
                try:
                    note = str(contact.note()) if contact.note() else ""
                except Exception:
                    note = ""

                apple_contact = AppleContact(
                    identifier=str(contact.identifier()),
                    given_name=str(contact.givenName()) if contact.givenName() else "",
                    family_name=str(contact.familyName()) if contact.familyName() else "",
                    nickname=str(contact.nickname()) if contact.nickname() else "",
                    organization=str(contact.organizationName()) if contact.organizationName() else "",
                    job_title=str(contact.jobTitle()) if contact.jobTitle() else "",
                    department=str(contact.departmentName()) if contact.departmentName() else "",
                    emails=self._extract_labeled_values(contact.emailAddresses()),
                    phones=self._extract_labeled_values(contact.phoneNumbers()),
                    addresses=self._extract_addresses(contact.postalAddresses()),
                    social_profiles=self._extract_social_profiles(contact.socialProfiles()),
                    note=note,
                    image_available=bool(contact.imageDataAvailable()),
                    birthday=birthday,
                )
                contacts.append(apple_contact)
            except Exception as e:
                logger.error(f"Error parsing contact: {e}")

        try:
            store.enumerateContactsWithFetchRequest_error_usingBlock_(
                request, None, enumerate_contact
            )
        except Exception as e:
            logger.error(f"Error fetching contacts: {e}")
            error = e

        if error:
            logger.warning(f"Contacts fetch completed with errors: {error}")

        return contacts


def create_contact_source_entity(contact: AppleContact) -> SourceEntity:
    """
    Create a SourceEntity from an Apple Contact.

    Args:
        contact: AppleContact object

    Returns:
        SourceEntity ready for storage
    """
    return SourceEntity(
        source_type=SOURCE_CONTACTS,
        source_id=contact.identifier,
        observed_name=contact.display_name,
        observed_email=contact.primary_email,
        observed_phone=contact.primary_phone,
        metadata={
            "given_name": contact.given_name,
            "family_name": contact.family_name,
            "nickname": contact.nickname,
            "organization": contact.organization,
            "job_title": contact.job_title,
            "department": contact.department,
            "emails": contact.emails,
            "phones": contact.phones,
            "social_profiles": contact.social_profiles,
            "note": contact.note if len(contact.note) < 500 else contact.note[:500] + "...",
            "image_available": contact.image_available,
            "birthday": contact.birthday.isoformat() if contact.birthday else None,
        },
        observed_at=datetime.now(timezone.utc),
    )


def sync_apple_contacts(
    entity_store: SourceEntityStore,
    reader: Optional[AppleContactsReader] = None,
) -> dict:
    """
    Sync Apple Contacts to SourceEntity store.

    Returns sync statistics.
    """
    if reader is None:
        reader = AppleContactsReader()

    stats = {
        "total": 0,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
    }

    if not reader.is_available:
        stats["error"] = "Apple Contacts not available on this platform"
        return stats

    auth_status = reader.check_authorization()
    if auth_status != "authorized":
        stats["error"] = f"Contacts access not authorized: {auth_status}"
        return stats

    contacts = reader.get_all_contacts()
    stats["total"] = len(contacts)

    for contact in contacts:
        try:
            # Skip contacts without a name
            if not contact.display_name or contact.display_name == contact.identifier:
                stats["skipped"] += 1
                continue

            source_entity = create_contact_source_entity(contact)

            # Check if entity already exists
            existing = entity_store.get_by_source(SOURCE_CONTACTS, contact.identifier)
            if existing:
                # Update metadata
                existing.observed_name = source_entity.observed_name
                existing.observed_email = source_entity.observed_email
                existing.observed_phone = source_entity.observed_phone
                existing.metadata = source_entity.metadata
                existing.observed_at = source_entity.observed_at
                entity_store.update(existing)
                stats["updated"] += 1
            else:
                entity_store.add(source_entity)
                stats["created"] += 1

        except Exception as e:
            logger.error(f"Error syncing contact {contact.identifier}: {e}")
            stats["errors"] += 1

    return stats


# Singleton reader instance
_reader: Optional[AppleContactsReader] = None


def get_contacts_reader() -> AppleContactsReader:
    """Get or create singleton contacts reader."""
    global _reader
    if _reader is None:
        _reader = AppleContactsReader()
    return _reader
