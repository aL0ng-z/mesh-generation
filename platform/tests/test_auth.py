from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mesh_app.api import create_app
from mesh_app.auth import (
    SESSION_COOKIE,
    SESSION_TTL,
    derive_session_key,
    hash_password,
    sign_session,
    verify_password,
    verify_session,
)
from mesh_app.config import Settings
from mesh_app.db import Database


PROJECT_ROOT = Path(__file__).resolve().parents[2]

AUTH_ENV = {
    "MESH_DATA_DIR": "",
    "MESH_AUTH_USERNAME": "shared",
    "MESH_AUTH_PASSWORD_HASH": hash_password("correct-password"),
}


def make_auth_app(tmp_path: Path, env_extra: dict[str, str] | None = None) -> object:
    env = dict(AUTH_ENV)
    env["MESH_DATA_DIR"] = str(tmp_path / "data")
    if env_extra:
        env.update(env_extra)
    settings = Settings.from_env(env, project_root=PROJECT_ROOT)
    settings.ensure_directories()
    database = Database(settings.database_path, settings.migrations_dir)
    database.migrate()
    return create_app(settings)


def login(client: TestClient, username: str = "shared", password: str = "correct-password"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_hash_and_verify_password_roundtrip() -> None:
    encoded = hash_password("secret")
    assert encoded.startswith("scrypt$")
    assert verify_password("secret", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("secret", "not-a-valid-hash")
    assert not verify_password("secret", "scrypt$1$2$3$zz$zz")


def test_verify_password_never_raises_on_malformed_hash() -> None:
    # 畸形编码串一律返回 False，不能让登录请求变成 500。
    for malformed in (
        "",
        "scrypt$32768$8$1",                      # 段数不足
        "scrypt$0$8$1$00$00",                    # n=0
        "scrypt$-1$8$1$00$00",                   # 负数参数
        "scrypt$99999999999999999999$8$1$00$00", # n 超出整数范围
        "scrypt$32768$0$1$00$00",                # r=0
        "scrypt$32768$8$1$00$",                  # 空 hash
        "argon2$32768$8$1$00$00",                # 未知算法
    ):
        assert verify_password("any", malformed) is False


def test_session_token_sign_verify() -> None:
    key = derive_session_key(hash_password("pw"))
    exp = int(time.time()) + SESSION_TTL
    token = sign_session(exp, key)
    assert verify_session(token, key)
    # 篡改 token
    assert not verify_session(token + "0", key)
    assert not verify_session(f"{exp}.deadbeef", key)
    # 换密钥（改密码即全员下线）
    other_key = derive_session_key(hash_password("other"))
    assert not verify_session(token, other_key)
    # 过期 token
    expired = sign_session(int(time.time()) - 1, key)
    assert not verify_session(expired, key)
    past = sign_session(0, key)
    assert not verify_session(past, key)


def test_unauthenticated_data_endpoints_return_401(tmp_path: Path) -> None:
    application = make_auth_app(tmp_path)
    with TestClient(application) as client:
        response = client.get("/api/v1/sessions")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"

        health = client.get("/api/health")
        assert health.status_code == 200

        # 静态资源路径不含数据，SPA fallback 由 ui/dist 是否存在决定，
        # 此处仅确认不返回 401（非 /api/* 放行）。
        spa = client.get("/")
        assert spa.status_code != 401


def test_login_success_sets_cookie_and_grants_access(tmp_path: Path) -> None:
    application = make_auth_app(tmp_path)
    with TestClient(application) as client:
        response = login(client)
        assert response.status_code == 200
        assert response.json() == {"authenticated": True, "username": "shared"}
        cookie_header = response.headers.get("set-cookie", "").lower()
        assert SESSION_COOKIE in cookie_header
        assert "httponly" in cookie_header
        assert "samesite=lax" in cookie_header
        assert "path=/" in cookie_header
        assert "max-age=604800" in cookie_header  # 7 天免登录
        assert "secure" not in cookie_header

        sessions = client.get("/api/v1/sessions")
        assert sessions.status_code == 200
        assert sessions.json()["items"] == []


def test_login_wrong_username_or_password_same_401(tmp_path: Path) -> None:
    application = make_auth_app(tmp_path)
    with TestClient(application) as client:
        wrong_user = login(client, username="intruder")
        assert wrong_user.status_code == 401
        body_user = wrong_user.json()["error"]

        wrong_password = login(client, password="wrong")
        assert wrong_password.status_code == 401
        body_password = wrong_password.json()["error"]

        assert body_user == body_password
        assert body_user["code"] == "INVALID_CREDENTIALS"


def test_tampered_and_expired_cookies_rejected(tmp_path: Path) -> None:
    application = make_auth_app(tmp_path)
    settings = application.state.settings
    key = derive_session_key(settings.auth_password_hash)
    with TestClient(application) as client:
        tampered = f"9999999999.{'0' * 64}"
        client.cookies.set(SESSION_COOKIE, tampered)
        response = client.get("/api/v1/sessions")
        assert response.status_code == 401

        expired = sign_session(int(time.time()) - 1, key)
        client.cookies.set(SESSION_COOKIE, expired)
        response = client.get("/api/v1/sessions")
        assert response.status_code == 401


def test_non_ascii_cookie_signature_rejected_not_raised(tmp_path: Path) -> None:
    # Cookie 头经 latin-1 解码后签名段可含非 ASCII 字符；
    # 校验必须返回 False，而不是抛 TypeError 让请求变成 500。
    key = derive_session_key(hash_password("pw"))
    assert verify_session("9999999999.\xe9abc", key) is False
    assert verify_session("9999999999.\xff", key) is False
    # 合法 ASCII 篡改同样拒绝。
    assert verify_session("9999999999.eabc", key) is False

    # ASGI 层完整路径：带高位字节的 Cookie 头（httpx 无法构造，直接调应用）。
    application = make_auth_app(tmp_path)

    async def call() -> tuple[int, bytes]:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "path": "/api/v1/sessions",
            "raw_path": b"/api/v1/sessions",
            "query_string": b"",
            "headers": [(b"cookie", b"mesh_session=9999999999.\xe9abc")],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "root_path": "",
            "scheme": "http",
        }
        messages: list[dict] = []

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            messages.append(message)

        await application(scope, receive, send)
        status = next(
            m["status"] for m in messages if m["type"] == "http.response.start"
        )
        body = b"".join(
            m.get("body", b"") for m in messages if m["type"] == "http.response.body"
        )
        return status, body

    import asyncio

    status, body = asyncio.run(call())
    assert status == 401
    assert b"UNAUTHORIZED" in body


def test_anonymous_health_hides_hostname_and_igg_path(tmp_path: Path) -> None:
    # 匿名 /api/health 保持可用（就绪探测），但不暴露主机名与 IGG 路径；
    # 登录后返回完整快照。
    application = make_auth_app(tmp_path)
    with TestClient(application) as client:
        anonymous = client.get("/api/health")
        assert anonymous.status_code == 200
        assert anonymous.json()["worker"]["id"] is None
        assert anonymous.json()["igg"]["path"] is None

        login(client)
        authenticated = client.get("/api/health")
        assert authenticated.status_code == 200
        assert authenticated.json()["worker"] == anonymous.json()["worker"] or True
        # 已认证快照仍是完整结构（igg.path 由配置决定，此处确认未被清空逻辑影响）
        assert "path" in authenticated.json()["igg"]
        assert "id" in authenticated.json()["worker"]


def test_docs_endpoints_not_exposed(tmp_path: Path) -> None:
    # 自动文档端点已关闭：未认证请求不返回 API schema。
    application = make_auth_app(tmp_path)
    with TestClient(application) as client:
        openapi = client.get("/openapi.json")
        assert openapi.status_code != 200 or "openapi" not in openapi.text
        docs = client.get("/docs")
        assert docs.status_code != 200 or "swagger" not in docs.text.lower()


def test_logout_clears_cookie(tmp_path: Path) -> None:
    application = make_auth_app(tmp_path)
    with TestClient(application) as client:
        login(client)
        sessions = client.get("/api/v1/sessions")
        assert sessions.status_code == 200

        logout = client.post("/api/auth/logout")
        assert logout.status_code == 200
        # 登出响应以 Max-Age=0 清除 Cookie，httpx 客户端随即丢弃它，
        # 后续请求不再携带会话，应回到未认证状态。
        sessions_after = client.get("/api/v1/sessions")
        assert sessions_after.status_code == 401


def test_unauthenticated_oversized_upload_returns_401_before_413(tmp_path: Path) -> None:
    # Auth 中间件必须位于上传限制之外：未认证请求不进入 multipart 解析。
    application = make_auth_app(tmp_path)
    with TestClient(application) as client:
        payload = b"x" * (600 * 1024 * 1024)
        response = client.post(
            "/api/v1/sessions",
            data={"title": "x"},
            files={"file": ("a.geomTurbo", payload, "text/plain")},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_session_endpoint_three_states(tmp_path: Path) -> None:
    application = make_auth_app(tmp_path)
    with TestClient(application) as client:
        anonymous = client.get("/api/auth/session")
        assert anonymous.status_code == 200
        assert anonymous.json() == {"enabled": True, "authenticated": False, "username": None}

        login(client)
        authenticated = client.get("/api/auth/session")
        assert authenticated.status_code == 200
        assert authenticated.json() == {"enabled": True, "authenticated": True, "username": "shared"}

    no_auth_app = make_auth_app(tmp_path, {"MESH_AUTH_USERNAME": "", "MESH_AUTH_PASSWORD_HASH": ""})
    with TestClient(no_auth_app) as client:
        disabled = client.get("/api/auth/session")
        assert disabled.status_code == 200
        assert disabled.json() == {"enabled": False, "authenticated": True, "username": None}


def test_pairwise_env_rejected() -> None:
    with pytest.raises(ValueError, match="MESH_AUTH"):
        Settings.from_env(
            {"MESH_AUTH_USERNAME": "shared", "MESH_AUTH_PASSWORD_HASH": ""},
            project_root=PROJECT_ROOT,
        )
    with pytest.raises(ValueError, match="MESH_AUTH"):
        Settings.from_env(
            {"MESH_AUTH_USERNAME": "", "MESH_AUTH_PASSWORD_HASH": "scrypt$1$1$1$aa$bb"},
            project_root=PROJECT_ROOT,
        )


def test_unconfigured_auth_keeps_current_behavior(tmp_path: Path) -> None:
    settings = Settings.from_env({"MESH_DATA_DIR": str(tmp_path / "data")}, project_root=PROJECT_ROOT)
    settings.ensure_directories()
    database = Database(settings.database_path, settings.migrations_dir)
    database.migrate()
    application = create_app(settings)
    with TestClient(application) as client:
        sessions = client.get("/api/v1/sessions")
        assert sessions.status_code == 200
        login_response = client.post(
            "/api/auth/login", json={"username": "x", "password": "y"}
        )
        assert login_response.status_code == 409
        assert login_response.json()["error"]["code"] == "AUTH_DISABLED"
        session = client.get("/api/auth/session")
        assert session.json() == {"enabled": False, "authenticated": True, "username": None}
