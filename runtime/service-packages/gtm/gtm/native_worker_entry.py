"""Stable file entry across Hermes PM re-exec; never read a packet before bootstrap."""
import sys
from pathlib import Path

CORE = Path('/home/hermes/.hermes/hermes-agent')
sys.path.insert(0, str(CORE))
import hermes_bootstrap  # Native launcher selects committed dependencies first.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gtm.briefing_daemon import native_worker

if __name__ == '__main__':
    native_worker()
