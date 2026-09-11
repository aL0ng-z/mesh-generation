"""FastAPI 入口、统一错误结构与 React SPA 托管。"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

from anyio import CapacityLimiter
from fastapi import FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from geomturbo import GeomTurboParseError, parse_geomturbo
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .artifacts import ArtifactStore, iter_file_range
from .auth import (
    AuthMiddleware,
    derive_session_key,
    SESSION_COOKIE,
    SESSION_TTL,
    sign_session,
    verify_password,
    verify_session,
)
from .config import Settings
from .control_service import ControlService
from .db import Database
from .schemas import (
    CompleteSessionRequest,
    ControlPreviewRequest,
    CreateRunRequest,
    ExperienceNoteRequest,
    LoginRequest,
    RetryRunRequest,
)
from .sessions import ServiceError, SessionService, load_json


LOGGER = logging.getLogger(__name__)
_MULTIPART_OVERHEAD_BYTES = 64 * 1024


class _UploadBodyTooLarge(Exception):
    pass


class _PreviewConversionGate:
    """预览转换门：同键请求共享一次生成（单飞），且每进程同时只执行一个转换。

    转换在 ``run_in_threadpool`` 中同步执行；等待在异步层完成，
    不阻塞事件循环。同键的迟到请求直接等待已开始的那次转换结果。
    """

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(1)
        self._inflight: dict[tuple, asyncio.Future] = {}

    async def run(self, key: tuple, function) -> Any:
        existing = self._inflight.get(key)
        if existing is not None:
            return await asyncio.shield(existing)
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._inflight[key] = future
        try:
            async with self._semaphore:
                result = await run_in_threadpool(function)
                future.set_result(result)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            if self._inflight.get(key) is future:
                del self._inflight[key]
        return future.result()


class _UploadBodyLimitMiddleware:
    """在 multipart 解析和临时文件落盘前限制上传请求的实际字节数。"""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int, max_file_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_file_bytes = max_file_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/api/v1/sessions"
        ):
            await self.app(scope, receive, send)
            return

        content_length = _content_length(scope)
        if content_length is not None and content_length > self.max_body_bytes:
            await self._reject(scope, receive, send)
            return

        received = 0
        exceeded = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    exceeded = True
                    raise _UploadBodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            # multipart 解析发生在响应开始前；若下层把 receive 异常转换为 500，
            # 丢弃该响应并由本层发送稳定的 413 错误结构。
            if not exceeded:
                await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except BaseException:
            if not exceeded:
                raise
        if exceeded:
            await self._reject(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = _error_response(
            413,
            "UPLOAD_TOO_LARGE",
            f"几何文件超过允许的 {self.max_file_bytes} 字节",
            {"max_bytes": self.max_file_bytes},
        )
        await response(scope, receive, send)


def _content_length(scope: Scope) -> int | None:
    values = [value for key, value in scope.get("headers", []) if key.lower() == b"content-length"]
    if len(values) != 1:
        return None
    try:
        value = int(values[0])
    except ValueError:
        return None
    return value if value >= 0 else None


def create_app(settings: Settings | None = None) -> FastAPI:
    """构建应用；启动时只检查迁移版本，不在请求路径执行 DDL。"""

    resolved_settings = settings or Settings.from_env()
    database = Database(
        resolved_settings.database_path,
        resolved_settings.migrations_dir,
        resolved_settings.busy_timeout_ms,
    )
    sessions = SessionService(database, project_root=resolved_settings.project_root)
    controls = ControlService(database, resolved_settings.data_dir)
    artifacts = ArtifactStore(
        database,
        resolved_settings.data_dir,
        geometry_dir=resolved_settings.geometry_dir,
        artifact_dir=resolved_settings.artifact_dir,
        max_upload_bytes=resolved_settings.max_upload_bytes,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        resolved_settings.ensure_directories()
        database.require_current()
        yield

    application = FastAPI(
        title="叶轮机械网格经验平台",
        version="0.1.0",
        lifespan=lifespan,
        # 内网部署关闭自动文档端点，避免未登录用户读取完整 API 结构。
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    application.add_middleware(
        _UploadBodyLimitMiddleware,
        max_body_bytes=resolved_settings.max_upload_bytes + _MULTIPART_OVERHEAD_BYTES,
        max_file_bytes=resolved_settings.max_upload_bytes,
    )
    application.add_middleware(
        AuthMiddleware,
        username=resolved_settings.auth_username,
        password_hash=resolved_settings.auth_password_hash,
    )
    application.state.settings = resolved_settings
    application.state.database = database
    application.state.sessions = sessions
    application.state.controls = controls
    application.state.artifacts = artifacts
    preview_gate = _PreviewConversionGate()
    application.state.preview_gate = preview_gate
    # 登录 scrypt 校验的专用容量限制器：等待在异步层排队，校验经线程池执行，
    # 不占用 ASGI 事件循环，也不挤占其他请求的线程池额度。
    scrypt_limiter = CapacityLimiter(resolved_settings.scrypt_max_concurrency)

    @application.exception_handler(ServiceError)
    async def handle_service_error(_request: Request, exc: ServiceError) -> JSONResponse:
        headers: dict[str, str] = {}
        if exc.status_code == 416 and isinstance(exc.details.get("size"), int):
            headers["Content-Range"] = f"bytes */{exc.details['size']}"
        return _error_response(exc.status_code, exc.code, exc.message, exc.details, headers=headers)

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details = {
            "fields": [
                {
                    "location": [str(part) for part in item.get("loc", ())],
                    "message": "字段格式或取值不符合要求",
                    "type": item.get("type", "validation_error"),
                }
                for item in exc.errors()
            ]
        }
        return _error_response(422, "VALIDATION_ERROR", "请求字段校验失败，请检查输入", details)

    @application.exception_handler(StarletteHTTPException)
    async def handle_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            message = "请求路径不存在"
        elif isinstance(exc.detail, str) and any("\u4e00" <= char <= "\u9fff" for char in exc.detail):
            message = exc.detail
        else:
            message = "请求无法处理，请检查路径和参数"
        return _error_response(
            exc.status_code,
            f"HTTP_{exc.status_code}",
            message,
            {},
            headers=dict(exc.headers or {}),
        )

    @application.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        LOGGER.exception("未处理的 API 内部异常", exc_info=exc)
        return _error_response(500, "INTERNAL_ERROR", "服务内部发生错误，请联系运维并查看服务日志", {})

    @application.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        snapshot = await run_in_threadpool(_health_snapshot, database, resolved_settings)
        # 鉴权启用时该端点保持匿名可访问（就绪探测），但匿名请求
        # 不暴露主机名与 IGG 完整路径，只返回状态计数。
        if resolved_settings.auth_password_hash is None:
            return snapshot
        token = request.cookies.get(SESSION_COOKIE)
        authenticated = (
            token is not None
            and verify_session(token, _session_key(resolved_settings))
        )
        if authenticated:
            return snapshot
        snapshot["worker"] = {**snapshot["worker"], "id": None}
        snapshot["igg"] = {**snapshot["igg"], "path": None}
        return snapshot

    @application.post("/api/auth/login")
    async def login(payload: LoginRequest, response: Response) -> dict[str, Any]:
        if resolved_settings.auth_password_hash is None:
            raise ServiceError("AUTH_DISABLED", "鉴权未配置，无需登录", status_code=409)
        username_match = hmac.compare_digest(
            payload.username.encode(),
            (resolved_settings.auth_username or "").encode(),
        )
        async with scrypt_limiter:
            password_match = await run_in_threadpool(
                verify_password, payload.password, resolved_settings.auth_password_hash
            )
        if not (username_match and password_match):
            raise ServiceError("INVALID_CREDENTIALS", "用户名或密码错误", status_code=401)
        token = sign_session(int(time.time()) + SESSION_TTL, _session_key(resolved_settings))
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=SESSION_TTL,
            httponly=True,
            samesite="lax",
            path="/",
            secure=resolved_settings.cookie_secure,
        )
        return {"authenticated": True, "username": resolved_settings.auth_username}

    @application.get("/api/auth/session")
    async def auth_session(request: Request) -> dict[str, Any]:
        if resolved_settings.auth_password_hash is None:
            return {"enabled": False, "authenticated": True, "username": None}
        token = request.cookies.get(SESSION_COOKIE)
        authenticated = (
            token is not None
            and verify_session(token, _session_key(resolved_settings))
        )
        return {
            "enabled": True,
            "authenticated": authenticated,
            "username": resolved_settings.auth_username if authenticated else None,
        }

    @application.post("/api/auth/logout")
    async def logout(response: Response) -> dict[str, Any]:
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"authenticated": False}

    @application.get("/api/v1/sessions")
    async def list_sessions(
        status: str | None = Query(default=None),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        return await run_in_threadpool(sessions.list_sessions, status=status, cursor=cursor, limit=limit)

    @application.post("/api/v1/sessions", status_code=201)
    async def create_session(
        file: UploadFile = File(...),
        title: str = Form(...),
        expert_signature: str | None = Form(default=None),
    ) -> dict[str, Any]:
        filename = file.filename or ""
        session_id = str(uuid.uuid4())
        baseline_id = str(uuid.uuid4())
        stored = None
        session_created = False
        try:
            stored = await run_in_threadpool(
                artifacts.save_geometry,
                file.file,
                session_id=session_id,
                filename=filename,
            )
            try:
                summary = await run_in_threadpool(parse_geomturbo, stored.path)
            except GeomTurboParseError as exc:
                raise ServiceError(
                    "INVALID_GEOMTURBO",
                    "几何文件超过安全解析边界，请检查文件是否为有效的 .geomTurbo",
                    status_code=422,
                    details=exc.details,
                ) from exc
            summary_data = summary.to_dict()
            summary_data["path"] = stored.relative_path
            if summary.row_count < 1:
                raise ServiceError(
                    "INVALID_GEOMTURBO",
                    "几何文件未解析到任何叶排，请确认文件是有效的 .geomTurbo",
                    status_code=422,
                )
            result = await run_in_threadpool(
                sessions.create_session,
                title=title,
                expert_name=expert_signature,
                source_filename=filename,
                geometry_sha256=stored.sha256,
                geometry_relative_path=stored.relative_path,
                geometry_summary=summary_data,
                session_id=session_id,
                baseline_id=baseline_id,
                geometry_artifact_size=stored.size_bytes,
                geometry_artifact_mime_type=stored.mime_type,
            )
            session_created = True
            return result
        except BaseException:
            if stored is not None and not session_created:
                await run_in_threadpool(artifacts.discard_unregistered, stored)
            raise
        finally:
            await file.close()

    @application.get("/api/v1/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        return await run_in_threadpool(sessions.get_session, session_id)

    @application.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        return await run_in_threadpool(sessions.get_run, run_id)

    @application.get("/api/v1/sessions/{session_id}/control-state")
    async def get_control_state(
        session_id: str,
        parent_run_id: str = Query(..., min_length=1),
    ) -> dict[str, Any]:
        return await run_in_threadpool(
            controls.get_control_state,
            session_id,
            parent_run_id=parent_run_id,
        )

    @application.post("/api/v1/sessions/{session_id}/control-preview")
    async def preview_controls(session_id: str, payload: ControlPreviewRequest) -> dict[str, Any]:
        preview = await run_in_threadpool(
            controls.preview,
            session_id,
            parent_run_id=payload.parent_run_id,
            changes=[item.model_dump(exclude_none=False) for item in payload.changes],
        )
        return _public_preview(preview)

    @application.post("/api/v1/sessions/{session_id}/runs", status_code=201)
    async def create_run(session_id: str, payload: CreateRunRequest) -> dict[str, Any]:
        snapshot, delta, _preview = await run_in_threadpool(
            controls.prepare_run_controls,
            session_id,
            parent_run_id=payload.parent_run_id,
            changes=[item.model_dump(exclude_none=False) for item in payload.changes],
            confirm_required_clears=payload.confirm_required_clears,
        )
        return await run_in_threadpool(
            sessions.create_child_run,
            session_id=session_id,
            parent_run_id=payload.parent_run_id,
            request_id=payload.request_id,
            expected_version=payload.expected_version,
            control_snapshot=snapshot,
            control_delta=delta,
        )

    @application.post("/api/v1/runs/{run_id}/retry", status_code=201)
    async def retry_run(run_id: str, payload: RetryRunRequest) -> dict[str, Any]:
        return await run_in_threadpool(
            sessions.retry_run,
            run_id=run_id,
            request_id=payload.request_id,
            expected_version=payload.expected_version,
        )

    @application.put("/api/v1/runs/{run_id}/experience-note")
    async def update_experience_note(run_id: str, payload: ExperienceNoteRequest) -> dict[str, Any]:
        return await run_in_threadpool(
            sessions.update_experience_note,
            run_id=run_id,
            note=payload.note,
            expected_note_version=payload.expected_note_version,
        )

    @application.post("/api/v1/sessions/{session_id}/complete")
    async def complete_session(session_id: str, payload: CompleteSessionRequest) -> dict[str, Any]:
        return await run_in_threadpool(
            sessions.complete_session,
            session_id=session_id,
            run_id=payload.run_id,
            expected_version=payload.expected_version,
        )

    @application.get("/api/v1/runs/{run_id}/events")
    async def get_events(
        run_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        return await run_in_threadpool(sessions.get_events, run_id, after=after, limit=limit)

    @application.get("/api/v1/artifacts/{artifact_id}")
    async def download_artifact(artifact_id: str, request: Request) -> StreamingResponse:
        metadata, path, byte_range = await run_in_threadpool(
            artifacts.open_download,
            artifact_id,
            request.headers.get("range"),
        )
        partial = bool((request.headers.get("range") or "").strip())
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(byte_range.length),
            "Content-Disposition": _content_disposition(str(metadata["display_name"])),
        }
        if partial:
            headers["Content-Range"] = f"bytes {byte_range.start}-{byte_range.end}/{byte_range.total}"
        return StreamingResponse(
            iter_file_range(path, byte_range),
            status_code=206 if partial else 200,
            media_type=metadata["mime_type"],
            headers=headers,
        )

    @application.get("/api/v1/runs/{run_id}/mesh/manifest")
    async def mesh_manifest(run_id: str) -> dict[str, Any]:
        preview_status = await run_in_threadpool(_run_preview_status, database, run_id)
        if preview_status == "PENDING":
            return {
                "status": "PENDING",
                "reason": "网格任务已成功，产物登记与预览后处理仍在进行",
                "blocks": [],
            }
        service = await run_in_threadpool(_preview_for_run, database, artifacts, resolved_settings, run_id)
        if service is None:
            return {"status": "UNAVAILABLE", "reason": "该运行尚无 CGNS 产物", "blocks": []}
        try:
            raw = await preview_gate.run(("manifest", run_id), service.manifest)
            return _public_manifest(raw)
        except Exception as exc:
            return _preview_failure_manifest(exc)

    @application.get("/api/v1/runs/{run_id}/mesh/blocks/{block}/{mode}")
    async def mesh_block(run_id: str, block: str, mode: str) -> FileResponse:
        service = await run_in_threadpool(_preview_for_run, database, artifacts, resolved_settings, run_id)
        if service is None:
            raise ServiceError("PREVIEW_UNAVAILABLE", "该运行尚无 CGNS 产物", status_code=409)
        try:
            path = await preview_gate.run(
                ("block", run_id, str(block), mode.lower()),
                lambda: service.block_asset(block, mode),
            )
        except Exception as exc:
            raise _preview_service_error(exc) from exc
        return FileResponse(path, media_type="application/vnd.vtk")

    @application.get("/api/v1/runs/{run_id}/mesh/slice")
    async def mesh_slice(
        run_id: str,
        block: str = Query(..., min_length=1),
        axis: str = Query(..., pattern="^[IJKijk]$"),
        index: int = Query(..., ge=0),
    ) -> FileResponse:
        service = await run_in_threadpool(_preview_for_run, database, artifacts, resolved_settings, run_id)
        if service is None:
            raise ServiceError("PREVIEW_UNAVAILABLE", "该运行尚无 CGNS 产物", status_code=409)
        try:
            path = await preview_gate.run(
                ("slice", run_id, str(block), axis.upper(), int(index)),
                lambda: service.slice_asset(
                    block,
                    axis,
                    index,
                    memory_budget_mb=resolved_settings.preview_memory_budget_mb,
                    cache_limit_mb=resolved_settings.preview_cache_limit_mb,
                ),
            )
        except Exception as exc:
            raise _preview_service_error(exc) from exc
        return FileResponse(path, media_type="application/vnd.vtk")

    assets_dir = resolved_settings.ui_dist_dir / "assets"
    if assets_dir.is_dir():
        application.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @application.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        if full_path == "api" or full_path.startswith("api/"):
            raise StarletteHTTPException(status_code=404)
        index_path = resolved_settings.ui_dist_dir / "index.html"
        if not index_path.is_file():
            raise StarletteHTTPException(status_code=404)
        return FileResponse(index_path, media_type="text/html")

    return application


def _session_key(settings: Settings) -> bytes:
    return derive_session_key(settings.auth_password_hash or "")


def _health_snapshot(database: Database, settings: Settings) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    stale_before = (now - timedelta(seconds=settings.worker_stale_seconds)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    with database.reading() as connection:
        queue = {
            row["status"]: row["count"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM runs GROUP BY status"
            ).fetchall()
        }
        worker = connection.execute(
            "SELECT * FROM workers ORDER BY heartbeat_at DESC LIMIT 1"
        ).fetchone()
    worker_online = worker is not None and worker["heartbeat_at"] >= stale_before
    resource_status = load_json(worker["resource_status_json"], {}) if worker is not None else {}
    igg_exists = settings.igg_path is not None and settings.igg_path.exists()
    return {
        "status": "ok" if worker_online and igg_exists else "degraded",
        "database": {"status": "ok", "version": database.current_version()},
        "worker": {
            "online": worker_online,
            "id": worker["id"] if worker is not None else None,
            "last_heartbeat": worker["heartbeat_at"] if worker is not None else None,
            "running_count": worker["running_count"] if worker is not None else 0,
            "max_concurrency": worker["max_concurrency"] if worker is not None else settings.max_concurrency,
        },
        "igg": {
            "configured": settings.igg_path is not None,
            "available": igg_exists,
            "path": str(settings.igg_path) if settings.igg_path is not None else None,
        },
        "queue": {
            "queued": queue.get("QUEUED", 0),
            "running": queue.get("RUNNING", 0),
            "succeeded": queue.get("SUCCEEDED", 0),
            "failed": queue.get("FAILED", 0),
        },
        "resource_gate": resource_status,
    }


def _preview_for_run(
    database: Database,
    artifacts: ArtifactStore,
    settings: Settings,
    run_id: str,
) -> Any | None:
    preview_status = _run_preview_status(database, run_id)
    if preview_status == "PENDING":
        raise ServiceError("PREVIEW_PENDING", "网格预览仍在生成，请稍后重试", status_code=409)
    with database.reading() as connection:
        artifact = connection.execute(
            """
            SELECT relative_path FROM artifacts
            WHERE run_id = ? AND upper(kind) = 'CGNS'
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
    if artifact is None:
        return None
    try:
        from .preview import PreviewService
    except ImportError as exc:
        raise ServiceError(
            "PREVIEW_DEPENDENCY_MISSING",
            "预览组件未安装，原始网格仍可下载",
            status_code=503,
        ) from exc
    cgns_path = artifacts.resolve_path(str(artifact["relative_path"]))
    return PreviewService(cgns_path, settings.preview_dir / run_id)


def _run_preview_status(database: Database, run_id: str) -> str:
    with database.reading() as connection:
        run = connection.execute(
            "SELECT preview_status FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
    if run is None:
        raise ServiceError("RUN_NOT_FOUND", "找不到指定运行", status_code=404)
    return str(run["preview_status"])


def _public_manifest(raw: dict[str, Any]) -> dict[str, Any]:
    blocks = []
    for block in raw.get("blocks", []):
        size = block.get("size") or [0, 0, 0]
        bounds = block.get("bounds") or {}
        ranges = block.get("index_ranges") or {}
        modes = set(block.get("modes") or [])
        blocks.append(
            {
                "id": block.get("id"),
                "name": block.get("name"),
                "dimensions": size,
                "bounds": [
                    *(bounds.get("x") or [0, 0]),
                    *(bounds.get("y") or [0, 0]),
                    *(bounds.get("z") or [0, 0]),
                ],
                "surface": "surface" in modes,
                "wireframe": "wireframe" in modes,
                "slices": {
                    axis: {"minimum": values[0], "maximum": values[1]}
                    for axis, values in ranges.items()
                },
            }
        )
    return {
        "status": raw.get("status", "UNAVAILABLE"),
        "reason": raw.get("reason"),
        "reason_code": raw.get("reason_code"),
        "format": raw.get("format"),
        "blocks": blocks,
    }


def _preview_failure_manifest(exc: Exception) -> dict[str, Any]:
    code = str(getattr(exc, "code", "PREVIEW_FAILED"))
    message = str(exc) if str(exc) else "网格预览生成失败，原始产物仍可下载"
    return {"status": "UNAVAILABLE", "reason": message, "reason_code": code, "blocks": []}


def _preview_service_error(exc: Exception) -> ServiceError:
    code = str(getattr(exc, "code", "PREVIEW_FAILED"))
    status_code = 404 if code in {"BLOCK_NOT_FOUND", "PREVIEW_ASSET_MISSING"} else 422
    return ServiceError(code, str(exc) or "网格预览请求失败", status_code=status_code)


def _public_preview(preview: dict[str, Any]) -> dict[str, Any]:
    return {
        key: preview.get(key)
        for key in (
            "valid",
            "normalized_changes",
            "expanded_changes",
            "required_clears",
            "warnings",
            "errors",
            "effective_availability",
        )
    }


def _content_disposition(filename: str) -> str:
    ascii_name = "".join(char if 32 <= ord(char) < 127 and char not in {'"', '\\'} else "_" for char in filename)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details}},
        headers=headers,
    )


def main() -> None:
    """控制台脚本入口。"""

    import uvicorn

    uvicorn.run("mesh_app.api:app", host="0.0.0.0", port=8000, reload=False)


app = create_app()


if __name__ == "__main__":
    main()


__all__ = ["_PreviewConversionGate", "app", "create_app", "main"]
