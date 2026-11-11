"""Absolute native launcher entry; bootstrap completes before private stdin is read."""
import sys
from pathlib import Path
if 'hermes_bootstrap' not in sys.modules:raise RuntimeError('native_runtime_command_bootstrap_required')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from background_jobs.native_worker import main
if __name__=='__main__':main()
