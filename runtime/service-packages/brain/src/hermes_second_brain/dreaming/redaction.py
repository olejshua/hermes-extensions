"""Redaction applied before any text reaches the model, state, or a report.

The goal is to remove credentials and one-time codes without destroying ordinary
numbers, which carry real meaning in the user's conversations (dates, prices,
counts, versions). Anything matched is replaced with a stable placeholder so the
surrounding sentence stays readable and the model can still reason about shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PLACEHOLDER = "[REDACTED]"
CODE_PLACEHOLDER = "[REDACTED-CODE]"
KEY_PLACEHOLDER = "[REDACTED-PRIVATE-KEY]"
EMAIL_PLACEHOLDER = "[REDACTED-EMAIL]"
PHONE_PLACEHOLDER = "[REDACTED-PHONE]"

# Every placeholder this module can emit. Quote validation uses this to reject
# "evidence" that is nothing but redaction markers.
ALL_PLACEHOLDERS: tuple[str, ...] = (
    PLACEHOLDER,
    CODE_PLACEHOLDER,
    KEY_PLACEHOLDER,
    EMAIL_PLACEHOLDER,
    PHONE_PLACEHOLDER,
)

# Keys whose *value* is always a secret, regardless of shape.
_SECRET_KEY_WORDS = (
    "api[_-]?key",
    "apikey",
    "access[_-]?key",
    "secret[_-]?key",
    "client[_-]?secret",
    "auth[_-]?token",
    "access[_-]?token",
    "refresh[_-]?token",
    "id[_-]?token",
    "bearer[_-]?token",
    "session[_-]?token",
    "private[_-]?key",
    "encryption[_-]?key",
    "signing[_-]?key",
    "password",
    "passwd",
    "passphrase",
    "kennwort",
    "passwort",
    "credential",
    "credentials",
    "secret",
    "token",
    "authorization",
    "auth",
    "cookie",
    "set[_-]?cookie",
    "x[_-]?api[_-]?key",
)
_SECRET_KEY_PATTERN = "|".join(_SECRET_KEY_WORDS)

# Phrases that make a nearby short digit run a one-time code rather than an
# ordinary number. A bare "code" is deliberately absent: this user writes about
# source code constantly, and "code 500" must survive as a status code.
_AUTH_CONTEXT = (
    r"(?:verification|verify|security|access|auth|authentication|login|sms|"
    r"one[-\s]?time|onetime|otp|2fa|mfa|totp|einmal\w*|bestätigungs?|bestaetigungs?|"
    r"sicherheits?|verifizierungs?|anmelde\w*)[-\s]?code",
    r"otp",
    r"2fa",
    r"mfa",
    r"totp",
    r"passcode",
    r"one[-\s]?time[-\s]?password",
    r"einmalpasswort",
    r"\bpin\b",
    r"\btan\b",
)
_AUTH_CONTEXT_PATTERN = "|".join(_AUTH_CONTEXT)

# A phone number is recognised by shape *and* by digit count. The regexes below
# are deliberately broad; ``_phone_replacement`` then rejects anything whose
# digit count is outside the E.164 range, which is what keeps ordinary dates,
# prices, version numbers, and IBAN fragments intact.
_PHONE_MIN_DIGITS = 7
_PHONE_MAX_DIGITS = 15
_NON_DIGIT_RE = re.compile(r"\D")

# International: an explicit +49 / 0049 style prefix followed by enough digits.
_PHONE_INTERNATIONAL = re.compile(r"(?<![\w+])(?:\+|00)[\s.\-/]?\d[\d\s().\-/]{5,20}\d(?![\w])")
# National trunk form: a leading zero, an area code of at least three digits in
# total, a separator, and a subscriber number. "01.07.2026" and "0.75" cannot
# match because both require digits, not punctuation, right after the zero.
_PHONE_NATIONAL = re.compile(
    r"(?<![\w+./\-])(?:0\d{2,5}[\s/\-]\d[\d\s/\-]{4,15}\d|"
    r"0(?:1[5-7]\d{8,10}|\d{9,12}))(?![\w])"
)

# Full email addresses. Credential URLs are handled earlier, so by the time this
# runs a ``user:pass@host`` form has already lost its userinfo.
_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?\.[A-Za-z]{2,24}\b")


def _phone_replacement(match: re.Match[str]) -> str:
    text = match.group(0)
    digits = _NON_DIGIT_RE.sub("", text)
    if not (_PHONE_MIN_DIGITS <= len(digits) <= _PHONE_MAX_DIGITS):
        return text
    return PHONE_PLACEHOLDER


_RULES: tuple[tuple[str, re.Pattern[str], object], ...] = (
    # Private key blocks: drop the entire armoured body.
    (
        "private_key_block",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        KEY_PLACEHOLDER,
    ),
    (
        "private_key_header",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        KEY_PLACEHOLDER,
    ),
    # Vendor-shaped tokens are unambiguous; match them before generic rules.
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"), PLACEHOLDER),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{16,}"), PLACEHOLDER),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"), PLACEHOLDER),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), PLACEHOLDER),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}"), PLACEHOLDER),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}"), PLACEHOLDER),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), PLACEHOLDER),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), PLACEHOLDER),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}"), PLACEHOLDER),
    ("stripe_key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"), PLACEHOLDER),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), PLACEHOLDER),
    # Authorization headers and bearer/basic credentials.
    (
        "authorization_header",
        re.compile(r"(?i)\b(authorization|proxy-authorization)\s*:\s*\S+.*", re.MULTILINE),
        lambda m: f"{m.group(1)}: {PLACEHOLDER}",
    ),
    (
        "bearer",
        re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{12,}"),
        lambda m: f"{m.group(1)} {PLACEHOLDER}",
    ),
    # Credential URLs: keep the host so the sentence still means something.
    (
        "credential_url",
        re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.\-]*)://([^\s/:@]+):([^\s/@]+)@"),
        lambda m: f"{m.group(1)}://{PLACEHOLDER}@",
    ),
    # Direct personal identifiers. These are not credentials, but they are the
    # PII most likely to end up quoted verbatim in a Dream artifact.
    ("email_address", _EMAIL, EMAIL_PLACEHOLDER),
    ("phone_international", _PHONE_INTERNATIONAL, _phone_replacement),
    ("phone_national", _PHONE_NATIONAL, _phone_replacement),
    # Generic secret assignments in JSON, YAML, env files, shell, and prose.
    (
        "secret_assignment_quoted",
        re.compile(
            rf'(?i)(["\']?\b(?:{_SECRET_KEY_PATTERN})\b["\']?\s*[:=]\s*)(["\'])([^"\'\n]{{3,}})(\2)'
        ),
        lambda m: f"{m.group(1)}{m.group(2)}{PLACEHOLDER}{m.group(4)}",
    ),
    (
        "secret_assignment_bare",
        re.compile(rf"(?i)(\b(?:{_SECRET_KEY_PATTERN})\b\s*[:=]\s*)([^\s,;&\"'\n]{{3,}})"),
        lambda m: f"{m.group(1)}{PLACEHOLDER}",
    ),
    (
        "secret_query_param",
        re.compile(rf"(?i)([?&](?:{_SECRET_KEY_PATTERN})=)([^&\s]{{3,}})"),
        lambda m: f"{m.group(1)}{PLACEHOLDER}",
    ),
    # One-time codes, only inside an authentication context.
    (
        "one_time_code",
        re.compile(
            rf"(?i)\b(?:{_AUTH_CONTEXT_PATTERN})\b[^\n\d\[]{{0,40}}\b(\d{{4,8}})\b"
        ),
        lambda m: m.group(0).replace(m.group(1), CODE_PLACEHOLDER),
    ),
    (
        "one_time_code_trailing",
        re.compile(
            rf"(?i)\b(\d{{4,8}})\b[^\n\d\[]{{0,25}}\b(?:{_AUTH_CONTEXT_PATTERN})\b"
        ),
        lambda m: m.group(0).replace(m.group(1), CODE_PLACEHOLDER, 1),
    ),
)


@dataclass(frozen=True)
class RedactionResult:
    text: str
    hits: tuple[str, ...]

    @property
    def redacted(self) -> bool:
        return bool(self.hits)


def redact(text: str) -> str:
    """Redact secrets from ``text``. Safe to call repeatedly (idempotent)."""

    return redact_detailed(text).text


def redact_detailed(text: str) -> RedactionResult:
    if not text:
        return RedactionResult("", ())
    result = text
    hits: list[str] = []
    for name, pattern, replacement in _RULES:
        previous = result
        result = pattern.sub(replacement, result)  # type: ignore[arg-type]
        # Compare text rather than trusting the substitution count: the phone
        # rules match broadly and then decline to replace when the digit count
        # says the match was a date or a price.
        if result != previous:
            hits.append(name)
    return RedactionResult(result, tuple(hits))


_WHITESPACE_RE = re.compile(r"\s+")


def normalize_for_quote_match(text: str) -> str:
    """Fold a quote or its source into a shape safe for exact substring checks.

    Only whitespace and case are normalised. Punctuation, digits, and word order
    are preserved, so this cannot turn a fabricated quote into a matching one.
    """

    return _WHITESPACE_RE.sub(" ", str(text)).strip().casefold()


def is_placeholder_only(text: str) -> bool:
    """True when a quote carries no real content beyond redaction markers."""

    stripped = re.sub(r"(?i)\[\s*redacted(?:-[a-z-]+)?\s*\]", " ", str(text))
    stripped = re.sub(r"(?i)\bredacted(?:-[a-z-]+)?\b", " ", stripped)
    return not _WHITESPACE_RE.sub("", stripped).strip(".,;:!?-–—…\"'()[]{}<>/\\|")


def contains_secret(text: str) -> bool:
    # Placeholders deliberately retain the secret field name for readability,
    # so some assignment regexes can count an idempotent replacement as a hit.
    # A value is unsafe only when another pass would actually change it.
    return redact(text) != text
