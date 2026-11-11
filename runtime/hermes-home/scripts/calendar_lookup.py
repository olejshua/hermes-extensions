"""Read-only batched meeting-start check; no model, reserve, invite or draft."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from calendar_availability import assess_exact_slot

BERLIN = ZoneInfo('Europe/Berlin')
CONTRACT = 'ole-calendar-check-v1'


def check(starts, *, service, calendar_ids, pending, clock):
    if not isinstance(starts, list) or not 1 <= len(starts) <= 32:
        raise ValueError('one_to_32_exact_starts_required')
    now = clock(); points = []
    for value in starts:
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError('aware_future_start_required')
        point = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if point.tzinfo is None or point.utcoffset() is None or point <= now:
            raise ValueError('aware_future_start_required')
        local = point.astimezone(BERLIN)
        # Local wall times must agree with their offset, including DST changes.
        if point.utcoffset() not in (timedelta(0), local.utcoffset()):
            raise ValueError('local_date_offset_mismatch')
        points.append(local)
    if max(points)-min(points) > timedelta(days=92):
        raise ValueError('split_calendar_window_required')
    lower = min(points)-timedelta(minutes=15)
    upper = max(points)+timedelta(minutes=30)
    rows = [{'start':p.isoformat(), 'state':'unknown', 'reason':'calendar_check_unavailable'} for p in points]
    try:
        svc = service(); ids = calendar_ids(svc)
        if len(ids) != 3 or len(set(ids)) != 3 or not all(isinstance(i,str) and i for i in ids):
            raise ValueError('three_distinct_verified_calendars_required')
        response = svc.freebusy().query(body={'timeMin':lower.isoformat(), 'timeMax':upper.isoformat(),
            'timeZone':'Europe/Berlin', 'items':[{'id':i} for i in ids]}).execute()
        checked = clock(); claims = tuple(pending())
        for row, point in zip(rows, points):
            assessment = assess_exact_slot(point, response, ids, checked_at=checked,
                                           now=clock(), pending=claims)
            row.update(state=assessment.status.value, reason=assessment.reason)
    except Exception:
        # Unknown API/identity/journal outcomes never masquerade as availability.
        for row in rows: row.update(state='unknown', reason='calendar_check_unavailable')
        return {'contract':CONTRACT, 'status':'unknown', 'checked_at':None,
                'slots':rows, 'reserved':False, 'hold_minutes':30}
    return {'contract':CONTRACT, 'status':'checked' if all(r['state']!='unknown' for r in rows) else 'unknown',
            'checked_at':checked.isoformat(), 'slots':rows, 'reserved':False, 'hold_minutes':30}
