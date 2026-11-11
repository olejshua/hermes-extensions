# Exact calendar evidence boundary

`availability.assess_exact_slot()` is a pure validation layer, not an active
Google adapter. Import it from the GTM adapter after obtaining a fresh freebusy
response for exactly the CODE primary and Fuxam calendar IDs. Supply the
response's `timeMin`, `timeMax`, `calendars`, the trusted local read timestamp,
current time, and every pending durable interval. Preserve the three states:
unknown blocks drafting; busy requires a real overlap; free still requires the
existing `mail_policy.scheduling()` hold+draft transaction and readback.

Do not exclude an own hold by string labels, company name, or recipient alone.
Use production `own_hold_available()` exact owner validation; the owner includes
account, thread, and sorted recipient set. A new caller must not relax that check.

The pure evaluator deliberately keeps the deployed start−15m/start+30m window.
It does not determine meeting duration, parse incoming mail, create invitations,
remove holds, refresh OAuth, or choose available times. The CODE+Fuxam identities
must be resolved completely (pagination, exactly one Fuxam) in the adapter.

Tests: `python3 -m unittest discover -s gtm/calendar -p 'test_*.py' -v`.
