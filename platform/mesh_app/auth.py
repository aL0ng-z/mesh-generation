"""共享密码登录：scrypt 编码/校验与无状态会话 Cookie。

未配置 ``MESH_AUTH_PASSWORD_HASH`` 时鉴权关闭，行为与现状一致。
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import os
import time

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

SCRYPT_N = 32768
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16
# 128*r*n = 32MiB，恰好触及 OpenSSL 默认 32MiB 限制，需显式放宽。
SCRYPT_MAXMEM = 64 * 1024 * 1024

SESSION_COOKIE = "mesh_session"
SESSION_TTL = 7 * 24 * 3600

_SESSION_KEY_PREFIX = "mesh-session:"


def hash_password(password: str) -> str:
    """生成 ``scrypt$n$r$p$salt_hex$hash_hex`` 编码串。"""
    salt = os.urandom(SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """校验明文密码与编码串是否匹配；畸形编码串一律视为不匹配。"""
    try:
        algo, n_str, r_str, p_str, salt_hex, hash_hex = encoded.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    try:
        n = int(n_str)
        r = int(r_str)
        p = int(p_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    try:
        dk = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        # n/r/p 越界或组合非法（如 0、负数、超出 maxmem）：编码串已损坏。
        return False
    return hmac.compare_digest(dk, expected)


def derive_session_key(password_hash: str) -> bytes:
    """从密码哈希派生 HMAC 签名密钥；改密码即换密钥，全员会话失效。"""
    return hashlib.sha256(f"{_SESSION_KEY_PREFIX}{password_hash}".encode()).digest()


def sign_session(exp: int, key: bytes) -> str:
    """签发 ``exp.signature`` 形式的无状态会话 token。"""
    payload = str(exp).encode()
    sig = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_session(token: str, key: bytes) -> bool:
    """校验会话 token 的签名与有效期。"""
    try:
        exp_str, sig = token.split(".", 1)
        exp = int(exp_str)
    except ValueError:
        return False
    expected = hmac.new(key, exp_str.encode(), hashlib.sha256).hexdigest()
    # Cookie 头经 latin-1 解码，签名段可能含非 ASCII 字符；str 版
    # compare_digest 会对其抛 TypeError，因此统一按字节比较。
    try:
        sig_match = hmac.compare_digest(sig.encode("latin-1"), expected.encode())
    except (UnicodeEncodeError, TypeError):
        return False
    if not sig_match:
        return False
    if exp <= time.time():
        return False
    return True


def _read_cookie(scope: Scope, name: str) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            if "=" not in part:
                continue
            cookie_name, cookie_value = part.strip().split("=", 1)
            if cookie_name == name:
                return cookie_value
    return None


async def _send_unauthorized(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse(
        status_code=401,
        content={"error": {"code": "UNAUTHORIZED", "message": "请先登录", "details": {}}},
    )
    await response(scope, receive, send)


class AuthMiddleware:
    """应用层共享密码鉴权：未配置即放行所有请求。

    放行：``/api/health``（就绪探测）、``/api/auth/*``（登录/会话/登出）
    与非 ``/api/*`` 静态资源；其余 ``/api/*`` 校验 ``mesh_session`` Cookie，
    失败返回 401 ``UNAUTHORIZED``。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        username: str | None,
        password_hash: str | None,
    ) -> None:
        self.app = app
        self.username = username
        self.password_hash = password_hash
        self.session_key = derive_session_key(password_hash) if password_hash else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.password_hash is None or scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/api/health" or path.startswith("/api/auth/") or not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        token = _read_cookie(scope, SESSION_COOKIE)
        if (
            token is not None
            and self.session_key is not None
            and verify_session(token, self.session_key)
        ):
            await self.app(scope, receive, send)
            return
        await _send_unauthorized(scope, receive, send)


def _generate_hash_cli() -> None:
    """交互式生成共享密码哈希，输出可直接粘贴进 ``.env``。"""
    print("为内网平台生成共享密码哈希（输入不会回显）")
    password = getpass.getpass("请输入共享密码: ")
    if not password:
        print("密码不能为空。")
        return
    confirm = getpass.getpass("请再次输入以确认: ")
    if password != confirm:
        print("两次输入不一致，已取消。")
        return
    encoded = hash_password(password)
    print()
    print(encoded)
    print()
    print("将上面这一行填入 .env 的 MESH_AUTH_PASSWORD_HASH，并设置 MESH_AUTH_USERNAME。")


if __name__ == "__main__":
    _generate_hash_cli()


__all__ = [
    "AuthMiddleware",
    "SESSION_COOKIE",
    "SESSION_TTL",
    "derive_session_key",
    "hash_password",
    "sign_session",
    "verify_password",
    "verify_session",
]
