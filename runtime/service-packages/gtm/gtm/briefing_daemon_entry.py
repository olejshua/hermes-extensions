"""Stable isolated Hermes PM entry for the read-only calendar daemon."""
import sys
from pathlib import Path

sys.path.insert(0, '/home/hermes/.hermes/hermes-agent')
import hermes_bootstrap
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gtm.briefing_daemon import main

if __name__ == '__main__':
    main()
