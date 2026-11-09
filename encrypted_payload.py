"""Compact storage for standard Fernet tokens; legacy encoded tokens stay readable."""
import base64

PREFIX=b'CSOENC1\x00'


def encrypt_content(cipher,content):
    return PREFIX+base64.urlsafe_b64decode(cipher.encrypt(content))


def decrypt_content(cipher,content):
    token=base64.urlsafe_b64encode(content[len(PREFIX):]) if content.startswith(PREFIX) else content
    return cipher.decrypt(token)
