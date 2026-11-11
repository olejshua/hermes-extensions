"""Account-bound Google credentials; one locked, durable writer per token path.

No mail/calendar writes. Only OAuth refresh and Gmail profile readback. Never log
exception prose or credential material. All Google dependencies are lazy imports
so the same implementation is exercised with private host fixtures.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time

EXPECTED = {
    "google_token_code.json": "operator@primary.example.invalid",
    "google_token_stealth.json": "operator@secondary.example.invalid",
    "google_token.json": "operator@primary.example.invalid",
}
OAUTH_ERRORS = frozenset({"invalid_grant", "invalid_client", "unauthorized_client", "invalid_scope", "access_denied", "invalid_request", "temporarily_unavailable", "server_error", "admin_policy_enforced"})
LOGIN_ERRORS = frozenset({"invalid_grant", "access_denied", "admin_policy_enforced"})
CLIENT_ERRORS = frozenset({"invalid_client", "unauthorized_client", "invalid_scope", "invalid_request"})


@dataclass(frozen=True)
class ProfileProof:
    email: str
    binding: str
    verified_monotonic: float

_PROFILE_CACHE = {}
PROFILE_PROOF_MAX_AGE_SECONDS = 30


def credential_binding(payload):
    return hashlib.sha256((generation(payload) + "|" + str(payload.get("token", ""))).encode()).hexdigest()


class AuthFailure(RuntimeError):
    def __init__(self, code, *, retryable=False):
        self.code, self.retryable = code, bool(retryable)
        super().__init__(code)


def safe_error(exc):
    """Allowlisted response fields, never str(exc), repr(exc) or response body."""
    if isinstance(exc, AuthFailure):
        return exc
    response = next((a for a in getattr(exc, "args", ()) if isinstance(a, dict)), {})
    code = response.get("error")
    if isinstance(code, str) and code in OAUTH_ERRORS:
        return AuthFailure(code, retryable=code in {"temporarily_unavailable", "server_error"})
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status in {429, 500, 502, 503, 504}:
        return AuthFailure("provider_unavailable", retryable=True)
    if status == 401:
        return AuthFailure("access_token_rejected", retryable=False)
    if status == 403:
        return AuthFailure("profile_access_unavailable", retryable=False)
    if type(exc).__name__ in {"TransportError", "ConnectionError", "Timeout", "TimeoutError", "ReadTimeout", "ConnectTimeout", "URLError"}:
        return AuthFailure("transport_unavailable", retryable=True)
    return AuthFailure("auth_operation_failed", retryable=True)


def generation(payload):
    """Private grant identity; ordinary access-token refresh does not invalidate a callback."""
    keys = ("client_id", "client_secret", "refresh_token", "token_uri", "scopes")
    identity = {k: payload.get(k) for k in keys}
    identity["scopes"] = sorted(identity.get("scopes") or [])
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def atomic_json(path, payload):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class AuthStore:
    def __init__(self, path, expected_email, *, credential_class, verify_profile, request_factory, verify_calendar=None, clock=time.time):
        self.path = Path(path)
        self.expected_email = expected_email.casefold()
        self.credential_class = credential_class
        self.verify_profile = verify_profile
        self.verify_calendar = verify_calendar
        self.request_factory = request_factory
        self.clock = clock
        self.lock_path = self.path.with_name(self.path.name + ".auth.lock")
        self.pending_path = self.path.with_name(self.path.name + ".refresh-pending.json")
        self.state_path = self.path.with_name(self.path.name + ".auth-state.json")
        self.authorization_path = self.path.with_name(self.path.name + ".authorization-pending.json")
        self.profile_proof = None

    @contextmanager
    def locked(self):
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
                raise AuthFailure("auth_lock_owner_mismatch")
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def read(self, path=None):
        path = self.path if path is None else Path(path)
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise AuthFailure("credential_permissions_invalid")
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = None
                data = json.load(stream)
        except AuthFailure:
            raise
        except Exception:
            raise AuthFailure("credential_unreadable") from None
        finally:
            if fd is not None:
                os.close(fd)
        if not isinstance(data, dict):
            raise AuthFailure("credential_unreadable")
        return data

    def validated(self, payload):
        if any(not isinstance(payload.get(k), str) or not payload[k] for k in ("client_id", "client_secret", "refresh_token")):
            raise AuthFailure("credential_not_refreshable")
        if payload.get("token_uri") != "https://oauth2.googleapis.com/token":
            raise AuthFailure("credential_endpoint_unapproved")
        scopes = payload.get("scopes")
        if not isinstance(scopes, list) or not scopes or any(not isinstance(x, str) or not x for x in scopes):
            raise AuthFailure("credential_scopes_invalid")
        return payload

    def verified(self, payload):
        try:
            actual = self.verify_profile(payload)
        except Exception as exc:
            raise safe_error(exc) from None
        proof = actual if isinstance(actual, ProfileProof) else ProfileProof(actual, credential_binding(payload), time.monotonic())
        if (not isinstance(proof.email, str) or proof.email.casefold() != self.expected_email or
                proof.binding != credential_binding(payload) or
                not 0 <= time.monotonic() - proof.verified_monotonic <= PROFILE_PROOF_MAX_AGE_SECONDS):
            raise AuthFailure("profile_mismatch")
        self.profile_proof = proof

    def _gate(self, payload):
        if not self.state_path.exists():
            return
        state = self.read(self.state_path)
        if state.get("generation") != generation(payload):
            return
        if state.get("terminal") or self.clock() < state.get("retry_after", 0):
            raise AuthFailure(state.get("code", "auth_review_required"), retryable=not state.get("terminal"))

    def _failure(self, payload, exc):
        failure = safe_error(exc)
        previous = self.read(self.state_path) if self.state_path.exists() else {}
        count = previous.get("failures", 0) + 1 if previous.get("generation") == generation(payload) else 1
        terminal = failure.code in LOGIN_ERRORS | CLIENT_ERRORS | {"profile_mismatch", "pending_generation_conflict"}
        atomic_json(self.state_path, {"generation": generation(payload), "code": failure.code, "terminal": terminal,
                    "failures": min(count, 20), "retry_after": 0 if terminal else self.clock() + min(300, 30 * 2 ** min(count - 1, 4))})
        raise failure from None

    def _clear(self):
        for path in (self.pending_path, self.state_path):
            path.unlink(missing_ok=True)
        sync_directory(self.path.parent)

    def _merge_refresh(self, old, candidate):
        new = json.loads(candidate.to_json())
        new["refresh_token"] = new.get("refresh_token") or old["refresh_token"]
        # A refresh never broadens scopes or changes the OAuth client/endpoint.
        for key in ("client_id", "client_secret", "token_uri"):
            if new.get(key) != old.get(key):
                raise AuthFailure("refresh_identity_changed")
        granted = getattr(candidate, "granted_scopes", None)
        if granted is not None and set(granted) != set(old["scopes"]):
            raise AuthFailure("refresh_scopes_changed")
        new["scopes"] = list(old["scopes"])
        new["account"] = self.expected_email
        self.validated(new)
        return new

    def _refresh_candidate(self, source, request):
        candidate = self.credential_class.from_authorized_user_info(source)
        candidate.refresh(request)
        if not candidate.valid:
            raise AuthFailure("refresh_result_invalid")
        payload = self._merge_refresh(source, candidate)
        return self.credential_class.from_authorized_user_info(payload), payload

    def _finish_pending(self, current, request):
        pending = self.read(self.pending_path)
        result = self.validated(pending.get("payload", {}))
        if (any(result.get(k) != current.get(k) for k in ("client_id", "client_secret", "token_uri")) or
                set(result["scopes"]) != set(current["scopes"])):
            raise AuthFailure("pending_identity_changed")
        if pending.get("source_generation") != generation(current):
            if credential_binding(current) != credential_binding(result):
                raise AuthFailure("pending_generation_conflict")
            # Primary publication succeeded before a crash during journal clear.
            # Recognize the exact result; never refresh an already completed write.
            result = current
        candidate = self.credential_class.from_authorized_user_info(result)
        if not candidate.valid:
            candidate, result = self._refresh_candidate(result, request)
            atomic_json(self.pending_path, {"source_generation": generation(current), "payload": result})
        self.verified(result)
        atomic_json(self.path, result)
        self._clear()
        return candidate

    def load(self):
        with self.locked():
            current = self.validated(self.read())
            self._gate(current)
            try:
                if self.pending_path.exists():
                    return self._finish_pending(current, self.request_factory())
                candidate = self.credential_class.from_authorized_user_info(current)
                if not candidate.valid:
                    candidate, result = self._refresh_candidate(current, self.request_factory())
                    # Persist a rotated refresh token before profile network I/O.
                    atomic_json(self.pending_path, {"source_generation": generation(current), "payload": result})
                    self.verified(result)
                    atomic_json(self.path, result)
                    self._clear()
                else:
                    self.verified(current)
                return candidate
            except Exception as exc:
                self._failure(current, exc)

    def refresh(self, previous, request):
        with self.locked():
            current = self.validated(self.read())
            self._gate(current)
            try:
                if self.pending_path.exists():
                    return self._finish_pending(current, request)
                candidate = self.credential_class.from_authorized_user_info(current)
                if candidate.valid and candidate.token != previous.token:
                    # Another process won; adopt only after account readback.
                    self.verified(current)
                    return candidate
                candidate, result = self._refresh_candidate(current, request)
                atomic_json(self.pending_path, {"source_generation": generation(current), "payload": result})
                self.verified(result)
                atomic_json(self.path, result)
                self._clear()
                return candidate
            except Exception as exc:
                self._failure(current, exc)

    def _authorization_record(self, payload, source_generation, *, refresh_tested=False, last_error=None):
        # Actor is the target, not a claim about the candidate's real account.
        # Unverified candidates are never returned by load() or profile receipts.
        private = dict(payload)
        private["account"] = ""
        return {"version": 1, "state": "unverified", "actor": self.expected_email,
                "source_path": self.path.name, "source_generation": source_generation,
                "payload": private, "refresh_tested": bool(refresh_tested), "last_error": last_error}

    def save_authorization(self, credentials, *, source_generation):
        """Journal an unverified received candidate before ANY profile/calendar I/O."""
        with self.locked():
            current = self.validated(self.read())
            if source_generation != generation(current):
                raise AuthFailure("stale_authorization_result")
            new = json.loads(credentials.to_json())
            granted = getattr(credentials, "granted_scopes", None)
            if granted is None:
                granted = new.get("scopes")
            if set(granted or []) != set(current["scopes"]):
                raise AuthFailure("authorization_scopes_changed")
            new["scopes"] = list(current["scopes"])
            self.validated(new)
            if any(new.get(k) != current.get(k) for k in ("client_id", "client_secret", "token_uri")):
                raise AuthFailure("authorization_client_changed")
            # An expired access token is locally recoverable from this newly
            # returned refresh grant; do not discard it before durable capture.
            if not isinstance(new.get("token"), str) or not new["token"]:
                raise AuthFailure("authorization_result_invalid")
            atomic_json(self.authorization_path, self._authorization_record(new, generation(current)))
            self._finish_authorization(current)

    def _authorization_probes(self, payload):
        self.verified(payload)
        if self.verify_calendar is not None:
            try:
                self.verify_calendar(payload)
            except Exception as exc:
                raise safe_error(exc) from None

    def _finish_authorization(self, current):
        pending = self.read(self.authorization_path)
        new = self.validated(pending.get("payload", {}))
        if (any(new.get(k) != current.get(k) for k in ("client_id", "client_secret", "token_uri")) or
                set(new["scopes"]) != set(current["scopes"])):
            raise AuthFailure("pending_identity_changed")
        if (pending.get("version") != 1 or pending.get("state") != "unverified" or
                pending.get("actor") != self.expected_email or pending.get("source_path") != self.path.name):
            raise AuthFailure("pending_actor_mismatch")
        if pending.get("source_generation") != generation(current):
            if credential_binding(new) != credential_binding(current):
                raise AuthFailure("stale_authorization_result")
            new = current  # exact publication won before crash during clear
        refresh_tested = pending.get("refresh_tested") is True
        try:
            candidate = self.credential_class.from_authorized_user_info(new)
            if not candidate.valid or pending.get("last_error") == "access_token_rejected":
                _, new = self._refresh_candidate(new, self.request_factory())
                refresh_tested = True
                atomic_json(self.authorization_path, self._authorization_record(new, generation(current), refresh_tested=True))
            # Bearer-only probes cannot perform an unjournaled auto refresh.
            self._authorization_probes(new)
            if not refresh_tested:
                _, new = self._refresh_candidate(new, self.request_factory())
                refresh_tested = True
                atomic_json(self.authorization_path, self._authorization_record(new, generation(current), refresh_tested=True))
                self._authorization_probes(new)  # bind both proofs to new access
            published = dict(new)
            published["account"] = self.expected_email
            atomic_json(self.path, published)
            self._clear()
            self.authorization_path.unlink(missing_ok=True)
            sync_directory(self.path.parent)
        except Exception as exc:
            failure = safe_error(exc)
            # Keep the exact candidate, including any durable rotation. This
            # does not gate or overwrite the older working published grant.
            atomic_json(self.authorization_path, self._authorization_record(new, generation(current), refresh_tested=refresh_tested, last_error=failure.code))
            raise failure from None

    def resume_authorization(self):
        with self.locked():
            current = self.validated(self.read())
            self._finish_authorization(current)


def google_store(path):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from google_auth_httplib2 import AuthorizedHttp
    import httplib2
    path = Path(path).expanduser().absolute()
    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser().absolute()
    if path.parent != home or path.name not in EXPECTED:
        raise AuthFailure("credential_path_unapproved")
    # RO baseline proved legacy and CODE share the exact same refresh grant.
    # One canonical account path prevents the default Workspace helper from
    # refreshing a stale duplicate independently of the active mail/calendar.
    if path.name == "google_token.json":
        path = home / "google_token_code.json"

    class BoundedRequest(Request):
        def __call__(self, *args, **kwargs):
            kwargs["timeout"] = 30
            return super().__call__(*args, **kwargs)

    def profile(payload):
        # Bare access credentials prevent an unjournaled refresh during readback.
        binding = credential_binding(payload)
        now = time.monotonic()
        cached = _PROFILE_CACHE.get(binding)
        if cached is not None and 0 <= now - cached.verified_monotonic <= PROFILE_PROOF_MAX_AGE_SECONDS:
            return cached
        bearer = Credentials(token=payload.get("token"))
        http = AuthorizedHttp(bearer, http=httplib2.Http(timeout=30), max_refresh_attempts=0)
        result = build("gmail", "v1", http=http, cache_discovery=False).users().getProfile(userId="me").execute(num_retries=0)
        proof = ProfileProof(result.get("emailAddress"), binding, time.monotonic())
        # Bounded process-only proof cache; never persist token hashes or identity.
        for key, old in list(_PROFILE_CACHE.items()):
            if now - old.verified_monotonic > PROFILE_PROOF_MAX_AGE_SECONDS:
                _PROFILE_CACHE.pop(key, None)
        _PROFILE_CACHE[binding] = proof
        return proof

    def calendar(payload):
        bearer = Credentials(token=payload.get("token"))
        http = AuthorizedHttp(bearer, http=httplib2.Http(timeout=30), max_refresh_attempts=0)
        build("calendar", "v3", http=http, cache_discovery=False).calendarList().list(maxResults=1).execute(num_retries=0)

    return AuthStore(path, EXPECTED[path.name], credential_class=Credentials, verify_profile=profile,
                     verify_calendar=calendar, request_factory=BoundedRequest)


def load_credentials_for_path(path):
    from google.oauth2.credentials import Credentials
    store = google_store(path)
    try:
        candidate = store.load()
    except Exception as exc:
        raise safe_error(exc) from None

    class StoredCredentials(Credentials):
        def refresh(self, request):
            try:
                fresh = self._auth_store.refresh(self, request)
            except Exception as exc:
                raise safe_error(exc) from None
            self.__dict__.update(fresh.__dict__)
            self._auth_profile_proof = self._auth_store.profile_proof

    result = StoredCredentials.from_authorized_user_info(json.loads(candidate.to_json()))
    result._auth_store = store
    result._auth_profile_proof = store.profile_proof
    return result


def save_authorization_for_path(path, credentials, *, source_generation):
    try:
        return google_store(path).save_authorization(credentials, source_generation=source_generation)
    except Exception as exc:
        raise safe_error(exc) from None


def verified_profile_for_service(service):
    """Exact current native credential proof; foreign/changed/old credentials miss."""
    credentials = getattr(getattr(service, "_http", None), "credentials", None)
    proof = getattr(credentials, "_auth_profile_proof", None)
    store = getattr(credentials, "_auth_store", None)
    if not isinstance(proof, ProfileProof) or not isinstance(store, AuthStore):
        return None
    try:
        valid = credentials.valid and credential_binding(json.loads(credentials.to_json())) == proof.binding
    except Exception:
        return None
    if (not valid or proof.email.casefold() != store.expected_email or
            not 0 <= time.monotonic() - proof.verified_monotonic <= PROFILE_PROOF_MAX_AGE_SECONDS):
        return None
    return proof.email


def resume_authorization_for_path(path):
    try:
        return google_store(path).resume_authorization()
    except Exception as exc:
        raise safe_error(exc) from None
