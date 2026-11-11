"""Absolute -I service entry; explicit package and pinned Brain source only."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'second-brain'/'src'))
sys.path.insert(0,str(ROOT))
from gtm.mail_observer.service import main
main()
