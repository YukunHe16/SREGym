"""Mask credentials in the text of a run.

Keys, tokens and private keys are found by their shape. Text is masked when a run is read, so a later cut cannot leave
half a key, and again in every request to a labeller, every cached answer and every report. Uses only the standard
library."""

from __future__ import annotations

import base64
import binascii
import re

CREDENTIALS = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"
    # a key printed only in part (``| head -5``): the header and the base64 lines after it
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[ \t]*\r?\n[ \t]*[A-Za-z0-9+/=]{8,})*"
    r"|\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}"  # JWTs: service-account and bearer tokens
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    r"|\b(?:sk|ghp|gho|ghs|ghu|ghr|xox[abps])[-_][\w-]{20,}"
    r"|\bgithub_pat_[\w]{20,}"
    r"|\bAIza[\w-]{35}"  # Google API keys
    r"|\bhf_[A-Za-z0-9]{30,}",  # Hugging Face tokens
    re.DOTALL,
)
BEARER = re.compile(r"(?i)(\bauthorization:\s*bearer\s+)[\w.~+/=-]{16,}")
# a token given by name, with no prefix of its own: kubectl's --token, a kubeconfig's ``token:``, a JSON "token" or
# "access_token". SREGym's own kube proxy hands the agent such a token (``secrets.token_urlsafe``).
TOKEN_FIELD = re.compile(r"""(?i)((?:--token(?:=|\s+)|(?<![a-z])token["']?\s*[:=]\s*)["']?)[\w.~+/-]{16,}=*""")
MASK = "[credential masked]"
# base64 of a PEM private key or of a JWT, as Kubernetes Secret data holds them, on one line or wrapped at 64 or 76
# columns (``base64``, ``openssl base64``). The whole block is decoded to tell a key from a certificate.
ENCODED = re.compile(
    r"(?<![A-Za-z0-9+/])(?:LS0tLS1CRUdJTi|ZXlK)[A-Za-z0-9+/=]{20,}(?:[ \t]*\r?\n[ \t]*[A-Za-z0-9+/=]{8,})*"
)


def _encoded(match: re.Match) -> str:
    """Mask base64 text that decodes to a private key or a JWT. Certificates are left as they are."""
    value = match.group(0)
    prefix = re.sub(r"\s", "", value)[:128]
    prefix = prefix[: len(prefix) - len(prefix) % 4]
    try:
        decoded = base64.b64decode(prefix, validate=True)
    except (ValueError, binascii.Error):
        return value
    secret = re.match(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----", decoded) or re.match(rb"eyJ[\w-]{10,}\.", decoded)
    return MASK if secret else value


def masked(text: str) -> str:
    """Return ``text`` with credentials replaced by the mask."""
    text = BEARER.sub(lambda m: m.group(1) + MASK, CREDENTIALS.sub(MASK, text))
    text = TOKEN_FIELD.sub(lambda m: m.group(1) + MASK, text)
    return ENCODED.sub(_encoded, text)


def masked_all(value):
    """Return ``value`` with every string masked, at any depth in dicts and lists, keys included."""
    if isinstance(value, str):
        return masked(value)
    if isinstance(value, dict):
        return {masked_all(k): masked_all(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [masked_all(v) for v in value]
    return value
