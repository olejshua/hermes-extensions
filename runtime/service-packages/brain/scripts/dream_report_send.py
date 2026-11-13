#!/usr/bin/env python3
"""Quiet no-model entry: next stored Dream report to the fixed configured destination, bound native receipt."""
import argparse
import sys
from pathlib import Path


def prepare_native_transport():
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv(hermes_home=Path('/home/hermes/.hermes'))
    from tools.send_message_tool import send_message_tool
    return send_message_tool


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("--config",type=Path,required=True)
    args=parser.parse_args(argv)
    if sys.version_info<(3,14) or "hermes_bootstrap" not in sys.modules:
        return 1
    from hermes_second_brain.dreaming.delivery import send_stored_report
    # No stdout. Existing no_agent/local cron must never duplicate delivery.
    result=send_stored_report(args.config,sender=prepare_native_transport())
    return 0 if result["state"] in {"disabled","quiet","delivered"} else 1


if __name__=="__main__":raise SystemExit(main())
