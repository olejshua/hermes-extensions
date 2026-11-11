#!/usr/bin/env python3
"""Compatibility entry for the existing appointment-status command (read only)."""
import sys
from pathlib import Path

# Local source tree and the actual reviewed installed GTM package. This does not
# import configs, start services or register a second calendar implementation.
ROOT = Path(__file__).resolve().parents[3]
if not (ROOT/'gtm/calendar/status.py').is_file():
    ROOT = Path('/home/hermes/.hermes/ole-reply-observer/releases/invitation-20261016-2/package')
sys.path.insert(0, str(ROOT))
from gtm.calendar.status import CONTRACT, main, report, source_read, connect

if __name__ == '__main__':
    main()
