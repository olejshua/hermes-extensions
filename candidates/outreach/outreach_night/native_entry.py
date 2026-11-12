"""Official native bootstrap entry; no auth/model invocation on import."""
import sys
import hashlib
from pathlib import Path
if 'hermes_bootstrap' not in sys.modules:raise RuntimeError('native_runtime_command_bootstrap_required')
root=Path(__file__).resolve().parents[1]
init=root/'background_jobs'/'__init__.py'
if (init.is_symlink() or not init.is_file() or hashlib.sha256(init.read_bytes()).hexdigest()!=
    '91858f74e01fb93c4f6b93bd763954800e2c8e352871df784e5e03bf69a477ca'):
    raise RuntimeError('reviewed_background_lazy_init_required')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from outreach_night.native_worker import main
if __name__=='__main__':main()
