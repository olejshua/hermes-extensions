"""Private native Desktop authority; tool arguments and SSH labels grant nothing."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path


@dataclass(frozen=True)
class DesktopIdentity:
    owner: str
    session: str
    binding: object
    frame: object

    def descriptor(self):
        return {
            'kind': 'desktop', 'binding_sha256': self.binding.fingerprint,
            'native_session_key': self.frame.session_key,
            'native_session_id': self.frame.session_id,
            'message_id': self.frame.message_id,
        }


def owner_binding(spec, policy, home):
    if not isinstance(spec, dict) or set(spec) != {'path', 'sha256'}:
        raise ValueError('verified_desktop_binding_required')
    path = Path(spec['path'])
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('verified_desktop_binding_required')
    from tui_gateway.plugin_continuation import read_owner_binding
    return read_owner_binding(path, spec['sha256'], owner=policy['account'], profile_home=home)


def session_key(binding, native_key, native_session_id):
    # Separate from every Telegram key; bind the durable native actor session too.
    value = [binding.fingerprint, native_key, native_session_id]
    return 'desktop:' + hashlib.sha256(json.dumps(value, separators=(',', ':')).encode()).hexdigest()


def identity(spec, policy, home, actor, native_session_id):
    binding = owner_binding(spec, policy, home)
    from tui_gateway.plugin_continuation import current
    frame = current(actor, native_session_id, binding)
    if frame is None or frame.request is not None:
        raise ValueError('native_private_session_required')
    return DesktopIdentity(policy['owner'], session_key(binding, frame.session_key, frame.session_id), binding, frame)
