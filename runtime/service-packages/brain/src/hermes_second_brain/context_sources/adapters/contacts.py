from __future__ import annotations

import datetime as dt
import sys
from collections.abc import Callable, Iterable
from itertools import islice
from typing import Any

from ..models import RawItem, ScanBatch, ScanRequest, Sensitivity, SourceCursor, SourceHealth, SourceStatus
from ..redaction import stable_hash

ContactProvider = Callable[[], Iterable[dict[str, Any]]]


class NativeContactsProvider:
    """Narrow public Contacts.framework reader; it never requests permission."""

    def __init__(self, framework: Any):
        self.framework = framework

    def authorized(self) -> bool:
        status = self.framework.CNContactStore.authorizationStatusForEntityType_(self.framework.CNEntityTypeContacts)
        return status == self.framework.CNAuthorizationStatusAuthorized

    def __call__(self) -> Iterable[dict[str, Any]]:
        if not self.authorized():
            raise PermissionError("contacts_permission_required")
        framework = self.framework
        keys = [
            framework.CNContactIdentifierKey,
            framework.CNContactEmailAddressesKey,
            framework.CNContactPhoneNumbersKey,
            framework.CNContactOrganizationNameKey,
        ]
        request = framework.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)
        store = framework.CNContactStore.alloc().init()
        contacts: list[dict[str, Any]] = []

        def collect(contact: Any, stop: Any) -> None:
            if len(contacts) >= 100_001:
                try:
                    stop[0] = True
                except Exception:
                    pass
                return
            emails = [str(item.value()) for item in islice(contact.emailAddresses(), 10)]
            phones = [str(item.value().stringValue()) for item in islice(contact.phoneNumbers(), 10)]
            contacts.append(
                {
                    "identifier": str(contact.identifier()),
                    "emails": emails,
                    "phones": phones,
                    "organization": str(contact.organizationName() or ""),
                }
            )

        result = store.enumerateContactsWithFetchRequest_error_usingBlock_(request, None, collect)
        if isinstance(result, tuple) and result and not result[0]:
            raise PermissionError("contacts_read_failed")
        return contacts


class ContactsAdapter:
    """Contacts.framework boundary; never reads Apple's private Contacts SQLite."""

    source_name = "contacts"
    sensitivity = Sensitivity.RESTRICTED
    retention = (30, 30)

    def __init__(self, *, provider: ContactProvider | None = None, native_provider_factory: Callable[[Any], NativeContactsProvider] = NativeContactsProvider):
        self.provider = provider
        self.native_provider_factory = native_provider_factory

    def _native_provider(self) -> NativeContactsProvider | None:
        if sys.platform != "darwin":
            return None
        try:
            framework = __import__("Contacts")
        except ImportError:
            return None
        return self.native_provider_factory(framework)

    def health(self, request: ScanRequest) -> SourceHealth:
        if self.provider is not None:
            return SourceHealth(self.source_name, SourceStatus.HEALTHY, "injected_provider", "contacts-framework-v1")
        if sys.platform != "darwin":
            return SourceHealth(self.source_name, SourceStatus.UNSUPPORTED, "contacts_framework_unavailable")
        native = self._native_provider()
        if native is None:
            return SourceHealth(self.source_name, SourceStatus.UNSUPPORTED, "pyobjc_contacts_unavailable")
        try:
            authorized = native.authorized()
        except Exception:
            return SourceHealth(self.source_name, SourceStatus.ERROR, "contacts_framework_error")
        if not authorized:
            return SourceHealth(self.source_name, SourceStatus.PENDING_PERMISSION, "contacts_permission_required")
        return SourceHealth(self.source_name, SourceStatus.HEALTHY, "ok", "contacts-framework-v1")

    def scan(self, request: ScanRequest, cursor: SourceCursor | None) -> ScanBatch:
        provider = self.provider or self._native_provider()
        if provider is None:
            raise PermissionError("contacts_framework_unavailable")
        now = request.now or dt.datetime.now(dt.timezone.utc)
        observed = now.isoformat().replace("+00:00", "Z")
        items: list[RawItem] = []
        skipped = 0
        continuing = bool(cursor and cursor.value.get("reconciling"))
        offset = int(cursor.value.get("record_index", 0)) if continuing and cursor else 0
        records = list(islice(provider(), offset + request.limit + 1))
        page = records[offset : offset + request.limit]
        has_more = len(records) > offset + request.limit
        for contact in page:
            if not isinstance(contact, dict):
                skipped += 1
                continue
            identifier = str(contact.get("identifier") or contact.get("id") or "")
            if not identifier or len(identifier.encode("utf-8")) > 1024:
                skipped += 1
                continue
            emails = contact.get("emails") if isinstance(contact.get("emails"), list) else []
            phones = contact.get("phones") if isinstance(contact.get("phones"), list) else []
            bounded_emails = [value for value in emails[:10] if isinstance(value, str) and len(value.encode("utf-8")) <= 512]
            bounded_phones = [value for value in phones[:10] if isinstance(value, str) and len(value.encode("utf-8")) <= 128]
            domains = sorted({value.rsplit("@", 1)[1].lower() for value in bounded_emails if "@" in value and len(value.rsplit("@", 1)[1]) <= 253})
            organization = contact.get("organization") if isinstance(contact.get("organization"), str) else ""
            if len(organization.encode("utf-8")) > 4096:
                organization = ""
            payload = {
                "contact_hash": stable_hash(identifier, namespace="contact"),
                "email_domains": domains[:10],
                "email_hashes": [stable_hash(value.lower(), namespace="contact-email") for value in bounded_emails],
                "phone_hashes": [stable_hash("".join(ch for ch in value if ch.isdigit()), namespace="contact-phone") for value in bounded_phones],
                "organization_hash": stable_hash(organization, namespace="contact-org") if organization else "",
            }
            items.append(RawItem(payload["contact_hash"], payload, observed))
        next_offset = offset + len(page)
        return ScanBatch(
            tuple(items),
            (),
            SourceCursor({"record_index": next_offset if has_more else 0, "reconciling": has_more, "reconciled_at": "" if has_more else observed}),
            "contacts-framework-v1",
            skipped,
            "partial" if has_more else "ok",
            full_reconciliation=True,
            scan_complete=not has_more,
        )
