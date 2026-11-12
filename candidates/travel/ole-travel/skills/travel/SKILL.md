---
name: travel
description: Current Berlin/VBB and Bahn journeys, native public-page fallback, exact places and times.
---

# Reiseauskunft

Use these tools for a matching travel request. Interpret once; fixed code executes the reads without another router/model. Do not load travel sources for unrelated conversation.

1. Resolve origin, destination, date, time, departure/arrival and explicit vehicle constraints. For “now”, use the current Europe/Berlin clock. Preserve endpoints and windows; never invent home/GPS, switch station/city, or reuse an old delay as current. Ask one short combined question only for a real ambiguity.
2. `travel_station_search` returns current candidates and `selected:null`. Use an exact confirmed ID/name. A named station is sufficient; broad “Berlin”, “Neustadt”, or multiple distinct places needs clarification. Never select a fuzzy first hit. API source is Community transport.rest; API failure falls back to the actual official public station form.
3. `travel_journeys` requires those IDs/names, exact aware time and explicit mode. `products` preserves only requested categories; omitted/empty means all. An unsupported official DB vehicle filter is blocked. `prefer_official` requests a real native public page. For a window, code reads to its boundary, error or declared page/deadline budget. This is provider search coverage, not every possible journey.
4. Read status, coverage, reason, acquisition time and actual source. Unknown delay/platform/cancellation stays unknown; an explicitly reported actual time equal to planned supports zero. Avoid cancelled/risky connections. Unparsed records remain in the complete original and make coverage partial. DB bot protection, login or changed layouts stop the read; do not bypass, repeatedly retry, or claim a successful website read after a blocked result.
5. If `requires_full_source_read:true`, follow every exact `next_offset` using `travel_result_read` on `result_source.source_ref`. Reassemble complete JSON before selecting a connection. Each packet fits complete native serialization including Unicode/escapes. Never advance by requested length. `original_sources` retains complete API bodies and machine-captured DOM frames. Receipts are immutable, scope-bound and expire after 10 minutes. Reading is not a new query, and a reference is not consumed evidence.
6. Answer briefly: departure/arrival, line, duration, changes and known delay or its unknown state, actual source and acquisition time. Use a captured source URL when useful. Keep relevant warnings; mention partial coverage in one sentence. If blocked, name the concrete obstacle. A generated link is not a successful lookup.
7. Timetable questions need no tariff profile. Alex is 20; never use a default adult 27–64 web profile for a personal price. Personal fares and cheapest-offer comparisons are not installed. Ask about BahnCard or Deutschlandticket only when a later actual fare task needs it. Never claim guaranteed/cheapest prices from timetable rows.
8. Maps lookup remains uninstalled: the current anonymous native read reached Google consent without a route result. Maps URLs are optional links, not completed lookups. Preserve explicit origin/mode; never default to GPS or claim URL generation as geocoding.

No accounts, keys, paid services, bookings, payments, saved trips, reminders, calendar/CRM writes or polling. Alex buys himself. No model-supplied DOM, sources, observations, JavaScript, credentials or paths enter these tools.
