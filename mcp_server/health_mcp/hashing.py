"""Hashes for secrets the connector stores: tokens, codes, invitations and passwords."""
import base64
import hashlib
import hmac
import secrets


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def hash_passphrase(passphrase, salt=None, n=2**15, r=8, p=1):
    """Format: scrypt$n$r$p$salt$hash, base64 url-safe."""
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(passphrase.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)
    encode = lambda b: base64.urlsafe_b64encode(b).decode().rstrip('=')
    return f'scrypt${n}${r}${p}${encode(salt)}${encode(derived)}'


def verify_passphrase(passphrase, stored):
    try:
        kind, n, r, p, salt, expected = stored.split('$')
        decode = lambda s: base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))
        assert kind == 'scrypt'
        actual = hash_passphrase(passphrase, decode(salt), int(n), int(r), int(p)).split('$')[-1]
        return hmac.compare_digest(actual, expected)
    except (ValueError, AssertionError):
        return False


# A fixed decoy so an unknown username costs the same time as a wrong password.
DECOY = hash_passphrase('decoy-' + secrets.token_hex(8))
