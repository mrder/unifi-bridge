"""
Dependency-free AES-128-GCM via ctypes calling directly into the system's
libcrypto (the shared library the `openssl` CLI itself is built on -- present
wherever openssl is installed, so this adds nothing new to a host/container
that already has the openssl binary).

Why: the controller's post-adoption inform encryption uses AES-128-GCM with a
16-byte nonce (not the usual 12) and AAD = the 6-byte device MAC address. The
plain `openssl enc` CLI command has no way to supply AAD or a custom nonce
length for an AEAD cipher, so it cannot reproduce this exactly -- this module
uses OpenSSL's low-level EVP API instead, which supports both.

On Linux (the real target -- Unraid/netshoot) this loads libcrypto.so(.3).
On Windows this is only used for local development/verification (loads
libcrypto-3-x64.dll if present, e.g. bundled with Git for Windows).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import platform


def _load_libcrypto():
    if platform.system() == "Windows":
        for candidate in (
            r"C:\Program Files\Git\mingw64\bin\libcrypto-3-x64.dll",
            "libcrypto-3-x64.dll",
            "libcrypto-1_1-x64.dll",
        ):
            try:
                return ctypes.CDLL(candidate)
            except OSError:
                continue
        raise OSError("libcrypto DLL not found")
    for name in ("libcrypto.so.3", "libcrypto.so.1.1", "libcrypto.so"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    found = ctypes.util.find_library("crypto")
    if found:
        return ctypes.CDLL(found)
    raise OSError("libcrypto not found")


_lib = _load_libcrypto()

_lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
_lib.EVP_aes_128_gcm.restype = ctypes.c_void_p
_lib.EVP_EncryptInit_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
_lib.EVP_DecryptInit_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
_lib.EVP_CIPHER_CTX_ctrl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
_lib.EVP_EncryptUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.c_void_p, ctypes.c_int]
_lib.EVP_DecryptUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.c_void_p, ctypes.c_int]
_lib.EVP_EncryptFinal_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
_lib.EVP_DecryptFinal_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
_lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]

EVP_CTRL_GCM_SET_IVLEN = 0x9
EVP_CTRL_GCM_GET_TAG = 0x10
EVP_CTRL_GCM_SET_TAG = 0x11
TAG_LEN = 16


def aes_128_gcm_encrypt(key: bytes, nonce: bytes, aad: bytes, plaintext: bytes) -> bytes:
    """Returns ciphertext with the 16-byte auth tag appended (matches the Java
    side's Cipher.doFinal() behavior for GCM, which appends the tag)."""
    ctx = _lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise RuntimeError("EVP_CIPHER_CTX_new failed")
    try:
        if _lib.EVP_EncryptInit_ex(ctx, _lib.EVP_aes_128_gcm(), None, None, None) != 1:
            raise RuntimeError("EncryptInit (cipher select) failed")
        if _lib.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_IVLEN, len(nonce), None) != 1:
            raise RuntimeError("set ivlen failed")
        if _lib.EVP_EncryptInit_ex(ctx, None, None, key, nonce) != 1:
            raise RuntimeError("EncryptInit (key/iv) failed")

        outlen = ctypes.c_int(0)
        if aad:
            if _lib.EVP_EncryptUpdate(ctx, None, ctypes.byref(outlen), aad, len(aad)) != 1:
                raise RuntimeError("AAD update failed")

        buf = ctypes.create_string_buffer(len(plaintext) + 16)
        total = 0
        if _lib.EVP_EncryptUpdate(ctx, buf, ctypes.byref(outlen), plaintext, len(plaintext)) != 1:
            raise RuntimeError("Encrypt update failed")
        total += outlen.value

        finlen = ctypes.c_int(0)
        if _lib.EVP_EncryptFinal_ex(ctx, ctypes.byref(buf, total), ctypes.byref(finlen)) != 1:
            raise RuntimeError("Encrypt final failed")
        total += finlen.value

        tag = ctypes.create_string_buffer(TAG_LEN)
        if _lib.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_GET_TAG, TAG_LEN, tag) != 1:
            raise RuntimeError("get tag failed")

        return buf.raw[:total] + tag.raw
    finally:
        _lib.EVP_CIPHER_CTX_free(ctx)


def aes_128_gcm_decrypt(key: bytes, nonce: bytes, aad: bytes, ciphertext_with_tag: bytes) -> bytes:
    if len(ciphertext_with_tag) < TAG_LEN:
        raise ValueError("ciphertext too short to contain a tag")
    ciphertext = ciphertext_with_tag[:-TAG_LEN]
    tag = ciphertext_with_tag[-TAG_LEN:]

    ctx = _lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise RuntimeError("EVP_CIPHER_CTX_new failed")
    try:
        if _lib.EVP_DecryptInit_ex(ctx, _lib.EVP_aes_128_gcm(), None, None, None) != 1:
            raise RuntimeError("DecryptInit (cipher select) failed")
        if _lib.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_IVLEN, len(nonce), None) != 1:
            raise RuntimeError("set ivlen failed")
        if _lib.EVP_DecryptInit_ex(ctx, None, None, key, nonce) != 1:
            raise RuntimeError("DecryptInit (key/iv) failed")

        outlen = ctypes.c_int(0)
        if aad:
            if _lib.EVP_DecryptUpdate(ctx, None, ctypes.byref(outlen), aad, len(aad)) != 1:
                raise RuntimeError("AAD update failed")

        buf = ctypes.create_string_buffer(len(ciphertext) + 16)
        total = 0
        if _lib.EVP_DecryptUpdate(ctx, buf, ctypes.byref(outlen), ciphertext, len(ciphertext)) != 1:
            raise RuntimeError("Decrypt update failed")
        total += outlen.value

        if _lib.EVP_CIPHER_CTX_ctrl(ctx, EVP_CTRL_GCM_SET_TAG, TAG_LEN, tag) != 1:
            raise RuntimeError("set tag failed")

        finlen = ctypes.c_int(0)
        ret = _lib.EVP_DecryptFinal_ex(ctx, ctypes.byref(buf, total), ctypes.byref(finlen))
        if ret != 1:
            raise ValueError("GCM authentication failed (bad tag/key/nonce/aad)")
        total += finlen.value

        return buf.raw[:total]
    finally:
        _lib.EVP_CIPHER_CTX_free(ctx)
