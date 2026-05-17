"""
AES-128-ECB 加解密工具（基于 pyca/cryptography）

iLink CDN 媒体用 AES-128-ECB + PKCS7 加密。不同段类型的 AES 密钥编码方式不同：

- **IMAGE**：``image_item.aeskey``（hex 字符串，16 字节 = 32 hex 字符）优先；
  fallback 用 ``image_item.media.aes_key``（base64 编码的 raw 16 字节）
- **VOICE / FILE / VIDEO**：``<type>_item.media.aes_key``（base64 编码的 raw 16 字节）

为避免混淆，本模块按"语义命名"提供两个解码函数：

- ``image_aes_key_from_hex(hex)`` — 给 IMAGE 用，从 hex 32 字符解码
- ``cdn_aes_key_from_base64(b64)`` — 给所有类型的 ``media.aes_key`` 用，从 base64 解码

加密侧：

- 一次性 API：``aes_ecb_encrypt`` / ``aes_ecb_decrypt``
- 流式 API：``StreamAESEncryptor`` / ``StreamAESDecryptor``
- 大小预算：``aes_ecb_padded_size(plaintext_size)`` 用于 ``getuploadurl`` 的 ``filesize`` 字段

**安全警告**：ECB 模式不具备语义安全性（相同明文块产生相同密文块）。
本项目仅为对接 iLink 协议的硬性要求而使用 ECB，不应用于任何新设计。
"""
import base64
import math
import os
from binascii import Error as BinasciiError
from typing import Final

from cryptography.hazmat.primitives.ciphers import Cipher, CipherContext, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7, PaddingContext

AES_KEY_LENGTH: Final[int] = 16
"""AES-128 密钥长度（字节）"""

_AES_BLOCK_SIZE_BITS: Final[int] = 128
_AES_BLOCK_SIZE_BYTES: Final[int] = 16


# ===== 大小预算 =====

def aes_ecb_padded_size(plaintext_size: int) -> int:
    """
    计算 AES-128-ECB + PKCS7 加密后的密文大小（字节）。

    协议公式（与官方 ``aes-ecb.ts:aesEcbPaddedSize`` 对齐）：
    ``ceil((plaintext_size + 1) / 16) * 16``

    PKCS7 padding 最少 1 字节、最多 16 字节，即使明文恰好 16 整数倍也要追加一个
    完整 16 字节块。这个值作为 ``getuploadurl`` 请求的 ``filesize`` 字段。

    :raises ValueError: plaintext_size 为负数
    """
    if plaintext_size < 0:
        raise ValueError(f"plaintext_size must be >= 0, got {plaintext_size}")
    return math.ceil((plaintext_size + 1) / _AES_BLOCK_SIZE_BYTES) * _AES_BLOCK_SIZE_BYTES


# ===== 密钥编码：明确语义版本 =====

def image_aes_key_from_hex(hex_str: str) -> bytes:
    """
    从 hex 字符串解码 IMAGE 段的 AES key（``image_item.aeskey``）。

    协议规定 IMAGE 的 ``aeskey`` 是 32 字符 hex 字符串（16 字节）。

    :raises ValueError: hex 字符串长度不为 32 或解码后非 16 字节
    """
    try:
        key = bytes.fromhex(hex_str)
    except ValueError as e:
        raise ValueError(f"Invalid hex in image aeskey: {e}") from e
    if len(key) != AES_KEY_LENGTH:
        raise ValueError(
            f"Image aeskey length mismatch: expected {AES_KEY_LENGTH} bytes, "
            f"got {len(key)} bytes (hex length {len(hex_str)})"
        )
    return key


def image_aes_key_to_hex(key: bytes) -> str:
    """把 16 字节 IMAGE AES key 编码为 hex 字符串（用于出站 ``image_item.aeskey``）"""
    if len(key) != AES_KEY_LENGTH:
        raise ValueError(f"AES key must be {AES_KEY_LENGTH} bytes, got {len(key)}")
    return key.hex()


def cdn_aes_key_from_base64(b64_str: str) -> bytes:
    """
    从 base64 字符串解码 CDN media 的 AES key（``media.aes_key``）。

    协议规定 ``CDNMedia.aes_key`` 是 base64 编码的 raw 16 字节。
    VOICE / FILE / VIDEO 段都用此函数；IMAGE 段的 fallback 路径也用此。

    :raises ValueError: 解码后非 16 字节
    """
    try:
        key = base64.b64decode(b64_str)
    except (ValueError, BinasciiError) as e:
        raise ValueError(f"Invalid base64 in media aes_key: {e}") from e
    if len(key) != AES_KEY_LENGTH:
        raise ValueError(
            f"Media aes_key length mismatch: expected {AES_KEY_LENGTH} bytes, "
            f"got {len(key)} bytes"
        )
    return key


def cdn_aes_key_to_base64(key: bytes) -> str:
    """把 16 字节 CDN media AES key 编码为 base64（用于出站 ``media.aes_key``）"""
    if len(key) != AES_KEY_LENGTH:
        raise ValueError(f"AES key must be {AES_KEY_LENGTH} bytes, got {len(key)}")
    return base64.b64encode(key).decode('ascii')


def generate_aes_key() -> bytes:
    """生成一个随机的 16 字节 AES-128 密钥（出站上传用本地生成）"""
    return os.urandom(AES_KEY_LENGTH)


# ===== 一次性 API（小数据用） =====

def aes_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """
    AES-128-ECB 一次性解密（带 PKCS7 unpadding）。

    适用于小数据（如单条文本消息的加密载荷）。大文件请用 ``StreamAESDecryptor``。

    :raises ValueError: 密文长度非 16 倍数 / PKCS7 填充损坏 / 密钥长度错
    """
    if len(key) != AES_KEY_LENGTH:
        raise ValueError(f"AES key must be {AES_KEY_LENGTH} bytes, got {len(key)}")
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    decryptor = cipher.decryptor()
    unpadder = PKCS7(_AES_BLOCK_SIZE_BITS).unpadder()

    decrypted = decryptor.update(ciphertext) + decryptor.finalize()
    return unpadder.update(decrypted) + unpadder.finalize()


def aes_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """
    AES-128-ECB 一次性加密（带 PKCS7 padding）。

    :raises ValueError: 密钥长度错
    """
    if len(key) != AES_KEY_LENGTH:
        raise ValueError(f"AES key must be {AES_KEY_LENGTH} bytes, got {len(key)}")
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    padder = PKCS7(_AES_BLOCK_SIZE_BITS).padder()

    padded = padder.update(plaintext) + padder.finalize()
    return encryptor.update(padded) + encryptor.finalize()


# ===== 流式 API（大文件用） =====

class StreamAESDecryptor:
    """
    AES-128-ECB 流式解密器（薄包装 ``cryptography.Cipher`` + PKCS7 unpadder）。

    不可复用：每次新的解密流必须创建新实例。``update`` 多次、``finalize`` 恰好一次。
    ``unpadder`` 会缓冲最后一块密文延迟输出，``finalize()`` 时做 PKCS7 去填充。

    :raises ValueError: 密钥长度错
    """

    _decryptor: CipherContext
    _unpadder: PaddingContext

    def __init__(self, key: bytes) -> None:
        if len(key) != AES_KEY_LENGTH:
            raise ValueError(f"AES key must be {AES_KEY_LENGTH} bytes, got {len(key)}")
        cipher = Cipher(algorithms.AES(key), modes.ECB())
        self._decryptor = cipher.decryptor()
        self._unpadder = PKCS7(_AES_BLOCK_SIZE_BITS).unpadder()

    def update(self, chunk: bytes) -> bytes:
        """喂入密文 chunk，返回已去填充的明文字节（可能为空）"""
        decrypted = self._decryptor.update(chunk)
        return self._unpadder.update(decrypted)

    def finalize(self) -> bytes:
        """
        处理剩余缓冲并返回末尾明文。

        :raises ValueError: 密文总长非 16 倍数 / PKCS7 填充损坏（密钥错 / 数据截断）
        """
        decrypted_tail = self._decryptor.finalize()
        return self._unpadder.update(decrypted_tail) + self._unpadder.finalize()


class StreamAESEncryptor:
    """
    AES-128-ECB 流式加密器（薄包装 ``cryptography.Cipher`` + PKCS7 padder）。

    不可复用：每次新的加密流必须创建新实例。

    :raises ValueError: 密钥长度错
    """

    _encryptor: CipherContext
    _padder: PaddingContext

    def __init__(self, key: bytes) -> None:
        if len(key) != AES_KEY_LENGTH:
            raise ValueError(f"AES key must be {AES_KEY_LENGTH} bytes, got {len(key)}")
        cipher = Cipher(algorithms.AES(key), modes.ECB())
        self._encryptor = cipher.encryptor()
        self._padder = PKCS7(_AES_BLOCK_SIZE_BITS).padder()

    def update(self, chunk: bytes) -> bytes:
        """喂入明文 chunk，返回密文字节（可能为空，最后一块被 padder 缓冲）"""
        padded = self._padder.update(chunk)
        return self._encryptor.update(padded)

    def finalize(self) -> bytes:
        """追加 PKCS7 padding（明文是 16 倍数时也追加完整块）并返回末尾密文"""
        padded_tail = self._padder.finalize()
        return self._encryptor.update(padded_tail) + self._encryptor.finalize()
