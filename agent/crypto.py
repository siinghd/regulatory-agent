"""Sealing secrets we must keep at rest: drop links (the URL fragment is the file key), drop
delete tokens, and queued outbound mail (which contains those links).

AES-256-GCM with a key derived (HKDF-SHA256) from DATA_ENCRYPTION_KEY, else AUDIT_HMAC_KEY, else
a random 32-byte key generated once into {data_dir}/keys/at-rest.key (mode 600). A database dump
alone therefore never reveals a working link.
"""

import base64
import os
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from agent.config import get_settings

_VERSION = b"\x01"
_NONCE_BYTES = 12
_INFO = b"regulatory-agent at-rest v1"


class SealError(Exception):
    """A sealed value can't be opened (wrong key, or tampered with)."""


def _key_file(data_dir: str) -> bytes:
    path = Path(data_dir) / "keys" / "at-rest.key"
    try:
        return path.read_bytes()
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = os.urandom(32)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    try:
        os.link(tmp, path)  # atomic and never overwrites: a concurrent first writer wins
    except FileExistsError:
        pass
    finally:
        tmp.unlink(missing_ok=True)
    return path.read_bytes()


@lru_cache(maxsize=8)
def _derive(material: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(material)


def _key() -> bytes:
    s = get_settings()
    material = s.data_encryption_key.get_secret_value() or s.audit_hmac_key.get_secret_value()
    return _derive(material.encode() if material else _key_file(s.data_dir))


def seal(plaintext: bytes, *, aad: bytes = b"") -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    return _VERSION + nonce + AESGCM(_key()).encrypt(nonce, plaintext, aad)


def unseal(sealed: bytes, *, aad: bytes = b"") -> bytes:
    if not sealed.startswith(_VERSION) or len(sealed) < 1 + _NONCE_BYTES + 16:
        raise SealError("not a sealed value")
    nonce, body = sealed[1 : 1 + _NONCE_BYTES], sealed[1 + _NONCE_BYTES :]
    try:
        return AESGCM(_key()).decrypt(nonce, body, aad)
    except Exception as e:  # InvalidTag: another key, or modified
        raise SealError("sealed value could not be opened") from e


def seal_text(text: str, *, aad: str = "") -> str:
    return base64.urlsafe_b64encode(seal(text.encode(), aad=aad.encode())).decode("ascii")


def unseal_text(sealed: str, *, aad: str = "") -> str:
    return unseal(base64.urlsafe_b64decode(sealed.encode("ascii")), aad=aad.encode()).decode()
