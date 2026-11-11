"""Private subprocess using the installed appointment reader; GET sources only.

The business calendar helper remains its owner's implementation. No model call,
business write, terminal command from model arguments, or source summarization.
"""
from __future__ import annotations

import argparse
import importlib.util
import importlib
import json
from pathlib import Path
import sys


def load_helper(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError('installed_read_helper_required')
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('dialogue_installed_appointment_reader', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The v2 compatibility entry exports main; the implementation lives in its
    # reviewed owner package. Accept only this exact known module, not a model path.
    if not callable(getattr(module, 'report', None)):
        if getattr(getattr(module, 'main', None), '__module__', None) != 'gtm.calendar.status':
            raise ValueError('installed_calendar_adapter_unavailable')
        module = importlib.import_module('gtm.calendar.status')
    return module


def run(helper, request):
    if request['kind'] == 'appointments':
        value = helper.report(request['date'], request.get('days', 1), None, True)
        if (not isinstance(value, dict) or value.get('contract') != 'ole-appointment-status-v2'
                or value.get('read_only') is not True or type(value.get('complete')) is not bool):
            raise ValueError('appointment_read_contract_invalid')
        return value
    if request['kind'] == 'mail_source':
        value = helper.source_read(request['account'], request['message_id'])
        if (not isinstance(value, dict) or value.get('contract') != 'ole-appointment-status-v2'
                or value.get('read_only') is not True or type(value.get('complete')) is not bool
                or value.get('account') != request['account'] or value.get('message_id') != request['message_id']):
            raise ValueError('mail_source_read_contract_invalid')
        return value
    raise ValueError('unsupported_source')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--helper', required=True)
    args = parser.parse_args()
    try:
        # Validate again at the subprocess boundary; no free-form process arguments.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from fast_reads import validate
        request = json.loads(sys.stdin.read(65537))
        request = validate({'requests': [request]})[0]
        result = run(load_helper(args.helper), request)
    except Exception:
        result = dict(read_only=True, complete=False, error='installed_source_unavailable')
    print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))


if __name__ == '__main__':
    main()
