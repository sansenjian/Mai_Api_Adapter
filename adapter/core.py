from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, ClassVar, Deque, Dict, List, Mapping, Optional
from uuid import uuid4

from aiohttp import web
from maibot_sdk import API, MaiBotPlugin, MessageGateway, PluginConfigBase

import asyncio
import hashlib
import json
import time
import traceback

from .settings import (
    DEFAULT_ACCOUNT_ID,
    DEFAULT_PLATFORM,
    DEFAULT_SCOPE,
    GATEWAY_NAME,
    HttpApiAdapterSettings,
)


class HttpApiAdapterPlugin(MaiBotPlugin):
    """Mai API 适配器插件。
    将 MaiBot 暴露为本地 Mai API，供其他软件通过 REST 接口调用。
    """

    config_model: ClassVar[type[PluginConfigBase] | None] = HttpApiAdapterSettings

    def __init__(self) -> None:
        super().__init__()
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._session_events: Dict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self._session_messages: Dict[str, Deque[Dict[str, Any]]] = {}
        self._internal_to_api_sessions: Dict[str, str] = {}
        self._running_host: Optional[str] = None
        self._running_port: Optional[int] = None

    async def on_load(self) -> None:
        await self._report_gateway_ready(True)
        await self._restart_server_if_needed()

    async def on_unload(self) -> None:
        await self._stop_server()
        await self._report_gateway_ready(False)

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        if scope != "self":
            return
        self.set_plugin_config(config_data)
        if version:
            self.ctx.logger.debug(f"Mai API 适配器收到配置更新: {version}")

        settings = self._load_settings()
        new_host = settings.server.host
        new_port = int(settings.server.port)
        should_listen = settings.should_listen()

        # 判断是否需要重启服务器：host/port 变了，或 should_listen 状态变了
        need_restart = (
            (self._running_host != new_host)
            or (self._running_port != new_port)
            or (should_listen and self._runner is None)
            or (not should_listen and self._runner is not None)
        )

        if need_restart:
            await self._stop_server()
            if should_listen:
                await self._restart_server_if_needed()
                await self._report_gateway_ready(True)
            else:
                await self._report_gateway_ready(False)
                self.ctx.logger.info("Mai API 适配器已停止监听（插件或 HTTP 服务未启用）")
        else:
            self.ctx.logger.debug("Mai API 配置已更新，无需重启服务器（host/port 未变）")

    @API("adapter.mai_api.health", description="获取 Mai API 适配器运行状态", version="1", public=True)
    async def api_health(self) -> Dict[str, Any]:
        return {
            "success": True,
            "adapter": "mai_api_adapter",
            "gateway": GATEWAY_NAME,
            "platform": DEFAULT_PLATFORM,
            "account_id": DEFAULT_ACCOUNT_ID,
            "scope": DEFAULT_SCOPE,
            "server": {
                "host": self._load_settings().server.host,
                "port": self._load_settings().server.port,
                "enabled": self._load_settings().server.enabled,
            },
        }

    @API("adapter.mai_api.send_message", description="通过 Mai API 向指定会话推送消息", version="1", public=True)
    async def api_send_message(self, session_id: Any, text: Any, user_id: Any = "", user_name: Any = "") -> Dict[str, Any]:
        normalized_session_id = str(session_id or "default").strip() or "default"
        normalized_text = str(text or "").strip()
        if not normalized_text:
            raise ValueError("text 不能为空")
        normalized_user_id = str(user_id or "api-user").strip() or "api-user"
        normalized_user_name = str(user_name or "API User").strip() or "API User"

        external_message_id = f"mai-api:{uuid4().hex}"
        accepted = await self._route_inbound_text(
            text=normalized_text,
            session_id=normalized_session_id,
            message_id=external_message_id,
            user_id=normalized_user_id,
            user_name=normalized_user_name,
        )
        return {
            "success": accepted,
            "session_id": normalized_session_id,
            "message_id": external_message_id,
            "accepted": accepted,
        }

    @MessageGateway(
        name=GATEWAY_NAME,
        route_type="duplex",
        platform=DEFAULT_PLATFORM,
        protocol="http",
        account_id=DEFAULT_ACCOUNT_ID,
        scope=DEFAULT_SCOPE,
        description="Local Mai API duplex message gateway",
    )
    async def handle_mai_api_gateway(
        self,
        message: Dict[str, Any],
        route: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        del route, metadata, kwargs

        session_id = self._session_id_from_message(message)

        item = {
            "id": str(message.get("message_id") or uuid4().hex),
            "session_id": session_id,
            "timestamp": time.time(),
            "text": self._extract_text(message),
            "message": message,
        }

        if self._load_settings().plugin.enable_debug_log:
            self.ctx.logger.debug(
                f"Mai API 出站: session={session_id} id={item['id']} text={item['text']!r}"
            )

        async with self._lock:
            history = self._get_history(session_id)
            history.append(item)
            self._session_events[session_id].set()
            self._session_events[session_id] = asyncio.Event()

        return {"success": True, "external_message_id": item["id"], "metadata": {"session_id": session_id}}

    async def _restart_server_if_needed(self) -> None:
        settings = self._load_settings()
        if not settings.should_listen():
            self.ctx.logger.info("Mai API 适配器保持空闲状态，因为插件或 HTTP 服务未启用")
            return

        app = web.Application(middlewares=[self._error_middleware, self._auth_middleware])
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/v1/messages", self._handle_post_message)
        app.router.add_get("/v1/messages/{session_id}", self._handle_get_messages)
        app.router.add_post("/v1/chat", self._handle_chat)
        app.router.add_post("/v1/chat/completions", self._handle_openai_chat_completions)
        app.router.add_get("/v1/models", self._handle_models)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        host = settings.server.host
        port = int(settings.server.port)
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()
        self._running_host = host
        self._running_port = port
        self.ctx.logger.info(f"Mai API adapter listening on http://{host}:{port}")

    async def _stop_server(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._running_host = None
        self._running_port = None

    async def _report_gateway_ready(self, ready: bool) -> bool:
        metadata: Dict[str, str] = {"protocol": "http"}
        if ready and self._running_host and self._running_port:
            metadata["server_url"] = f"http://{self._running_host}:{self._running_port}"
        try:
            return await self.ctx.gateway.update_state(
                gateway_name=GATEWAY_NAME,
                ready=ready,
                platform=DEFAULT_PLATFORM,
                account_id=DEFAULT_ACCOUNT_ID,
                scope=DEFAULT_SCOPE,
                metadata=metadata,
            )
        except Exception as exc:
            self.ctx.logger.warning(f"Mai API 网关状态上报失败: {exc}")
            return False

    def _load_settings(self) -> HttpApiAdapterSettings:
        return self.config  # type: ignore[return-value]

    @web.middleware
    async def _error_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        """捕获所有未处理的异常，统一返回 JSON 格式错误。"""
        try:
            return await handler(request)
        except web.HTTPException:
            raise  # aiohttp 自带的 HTTP 异常（400/401/404 等）保持原样
        except Exception as exc:
            error_body: Dict[str, Any] = {
                "error": {
                    "message": str(exc) or "internal server error",
                    "type": "internal_error",
                }
            }
            if self._load_settings().plugin.enable_debug_log:
                error_body["error"]["traceback"] = traceback.format_exc()
                self.ctx.logger.warning(f"Mai API 请求异常: {request.path}\n{traceback.format_exc()}")
            else:
                self.ctx.logger.warning(f"Mai API 请求异常: {request.path} {exc}")
            return web.json_response(error_body, status=500)

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        token = str(self._load_settings().server.token or "").strip()
        if token and request.path != "/health":
            header_token = str(request.headers.get("X-MaiBot-Token") or "").strip()
            auth_header = str(request.headers.get("Authorization") or "").strip()
            bearer_token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
            query_token = str(request.query.get("token") or "").strip()
            if token not in {header_token, bearer_token, query_token}:
                return web.json_response({"success": False, "error": "unauthorized"}, status=401)
        return await handler(request)

    async def _handle_health(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(
            {
                "success": True,
                "adapter": "mai_api_adapter",
                "gateway": GATEWAY_NAME,
                "platform": DEFAULT_PLATFORM,
                "account_id": DEFAULT_ACCOUNT_ID,
                "scope": DEFAULT_SCOPE,
            }
        )

    async def _handle_models(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {
                        "id": "maibot-mai-api",
                        "object": "model",
                        "created": 1700000000,
                        "owned_by": "maibot",
                    }
                ],
            }
        )

    async def _handle_post_message(self, request: web.Request) -> web.Response:
        payload = await self._read_json(request)
        filter_response = self._check_user_filter(payload)
        if filter_response is not None:
            return filter_response
        text = self._require_text(payload)
        session_id = self._normalize_session_id(payload)
        external_message_id = self._normalize_message_id(payload)

        accepted = await self._route_inbound_text(
            text=text,
            session_id=session_id,
            message_id=external_message_id,
            user_id=str(payload.get("user_id") or "api-user").strip() or "api-user",
            user_name=str(payload.get("user_name") or "API User").strip() or "API User",
        )
        return web.json_response(
            {
                "success": accepted,
                "session_id": session_id,
                "message_id": external_message_id,
                "accepted": accepted,
            },
            status=200 if accepted else 503,
        )

    async def _handle_get_messages(self, request: web.Request) -> web.Response:
        session_id = str(request.match_info.get("session_id") or "").strip()
        since = str(request.query.get("since") or "").strip()
        try:
            wait_seconds = float(request.query.get("wait") or 0)
        except ValueError:
            wait_seconds = 0

        if wait_seconds > 0:
            await self._wait_for_messages(session_id, wait_seconds)

        messages = list(self._get_history(session_id))
        if since:
            messages = [message for message in messages if str(message.get("id") or "") > since]
        return web.json_response({"success": True, "session_id": session_id, "messages": messages})

    async def _handle_chat(self, request: web.Request) -> web.Response:
        payload = await self._read_json(request)
        filter_response = self._check_user_filter(payload)
        if filter_response is not None:
            return filter_response
        text = self._require_text(payload)
        session_id = self._normalize_session_id(payload)
        external_message_id = self._normalize_message_id(payload)
        before_count = len(self._get_history(session_id))
        timeout_sec = self._normalize_timeout(payload)

        accepted = await self._route_inbound_text(
            text=text,
            session_id=session_id,
            message_id=external_message_id,
            user_id=str(payload.get("user_id") or "api-user").strip() or "api-user",
            user_name=str(payload.get("user_name") or "API User").strip() or "API User",
        )
        if not accepted:
            return web.json_response(
                {
                    "success": False,
                    "session_id": session_id,
                    "message_id": external_message_id,
                    "error": "message was not accepted by MaiBot",
                },
                status=503,
            )

        replies = await self._wait_for_new_messages(session_id, before_count, timeout_sec)
        return web.json_response(
            {
                "success": True,
                "session_id": session_id,
                "message_id": external_message_id,
                "replies": replies,
                "reply_text": "\n".join(item.get("text") or "" for item in replies if item.get("text")),
            }
        )

    async def _handle_openai_chat_completions(self, request: web.Request) -> web.StreamResponse:
        payload = await self._read_json(request)
        filter_response = self._check_user_filter(payload)
        if filter_response is not None:
            return filter_response
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise web.HTTPBadRequest(text=json.dumps({"error": {"message": "messages is required"}}))

        text = self._extract_last_user_text(messages)
        if not text:
            raise web.HTTPBadRequest(text=json.dumps({"error": {"message": "last user message is empty"}}))

        system_prompt = self._extract_system_messages(messages)
        # OpenAI 标准 user 字段优先，兼容自定义 user_id/user_name
        user_id = str(payload.get("user") or payload.get("user_id") or "api-user").strip() or "api-user"
        user_name = str(payload.get("user_name") or user_id).strip() or user_id
        session_id = self._normalize_session_id(payload)
        external_message_id = self._normalize_message_id(payload)
        before_count = len(self._get_history(session_id))
        timeout_sec = self._normalize_timeout(payload)
        model_name = str(payload.get("model") or "maibot-mai-api")

        accepted = await self._route_inbound_text(
            text=text,
            session_id=session_id,
            message_id=external_message_id,
            user_id=user_id,
            user_name=user_name,
            system_prompt=system_prompt,
        )
        if not accepted:
            return web.json_response(
                {"error": {"message": "message was not accepted by MaiBot", "type": "maibot_error"}},
                status=503,
            )

        replies = await self._wait_for_new_messages(session_id, before_count, timeout_sec)
        reply_text = "\n".join(item.get("text") or "" for item in replies if item.get("text"))
        response_id = f"chatcmpl-{uuid4().hex}"
        created = int(time.time())

        # 流式响应
        if payload.get("stream"):
            return await self._stream_reply(
                request=request,
                response_id=response_id,
                created=created,
                model_name=model_name,
                reply_text=reply_text,
                input_text=text,
            )

        # 非流式响应
        prompt_tokens = len(text) * 3 // 2
        completion_tokens = len(reply_text) * 3 // 2
        return web.json_response(
            {
                "id": response_id,
                "object": "chat.completion",
                "created": created,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": reply_text},
                        "finish_reason": "stop" if reply_text else "timeout",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
        )

    async def _stream_reply(
        self,
        *,
        request: web.Request,
        response_id: str,
        created: int,
        model_name: str,
        reply_text: str,
        input_text: str,
    ) -> web.StreamResponse:
        """将完整回复拆分为 SSE chunk 事件流式发送。"""
        settings = self._load_settings().server
        chunk_size = max(1, settings.stream_chunk_size)
        interval_sec = max(0, settings.stream_interval_ms) / 1000.0

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)

        # 逐块发送回复文本
        for i in range(0, len(reply_text), chunk_size):
            chunk = reply_text[i : i + chunk_size]
            data = json.dumps(
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": chunk},
                            "finish_reason": None,
                        }
                    ],
                },
                ensure_ascii=False,
            )
            await resp.write(f"data: {data}\n\n".encode("utf-8"))
            if interval_sec > 0:
                await asyncio.sleep(interval_sec)

        # 结束标记
        finish_data = json.dumps(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop" if reply_text else "timeout",
                    }
                ],
            }
        )
        await resp.write(f"data: {finish_data}\n\n".encode("utf-8"))
        await resp.write(b"data: [DONE]\n\n")
        return resp

    async def _route_inbound_text(
        self,
        *,
        text: str,
        session_id: str,
        message_id: str,
        user_id: str,
        user_name: str,
        system_prompt: str = "",
    ) -> bool:
        if self._load_settings().plugin.enable_debug_log:
            self.ctx.logger.debug(
                f"Mai API 入站: session={session_id} user={user_id}({user_name}) text={text!r}"
            )
        now = time.time()
        internal_user_id = self._build_internal_user_id(user_id=user_id, session_id=session_id)
        await self._remember_session_mapping(api_session_id=session_id, internal_user_id=internal_user_id)

        additional_config: Dict[str, Any] = {
            "platform_io_account_id": DEFAULT_ACCOUNT_ID,
            "platform_io_scope": DEFAULT_SCOPE,
            "platform_io_target_user_id": internal_user_id,
            "mai_api_session_id": session_id,
            "mai_api_user_id": user_id,
        }
        if system_prompt:
            additional_config["mai_api_system_prompt"] = system_prompt

        message_dict = {
            "message_id": message_id,
            "timestamp": str(now),
            "platform": DEFAULT_PLATFORM,
            "message_info": {
                "user_info": {
                    "user_id": internal_user_id,
                    "user_nickname": user_name,
                    "user_cardname": None,
                },
                "additional_config": additional_config,
            },
            "raw_message": [{"type": "text", "data": text}],
            "is_mentioned": True,
            "is_at": False,
            "is_emoji": False,
            "is_picture": False,
            "is_command": text.startswith("/"),
            "is_notify": False,
            "session_id": session_id,
            "processed_plain_text": text,
        }
        return await self.ctx.gateway.route_message(
            gateway_name=GATEWAY_NAME,
            message=message_dict,
            route_metadata={
                "platform": DEFAULT_PLATFORM,
                "account_id": DEFAULT_ACCOUNT_ID,
                "scope": DEFAULT_SCOPE,
                "platform_io_target_user_id": internal_user_id,
                "mai_api_session_id": session_id,
                "mai_api_user_id": user_id,
            },
            external_message_id=message_id,
            dedupe_key=message_id,
        )

    async def _read_json(self, request: web.Request) -> Dict[str, Any]:
        raw = await request.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Windows 上 curl 等工具可能用 GBK 编码发送中文
            try:
                text = raw.decode("gbk")
            except UnicodeDecodeError:
                raise web.HTTPBadRequest(
                    text=json.dumps({"success": False, "error": "invalid request encoding"})
                )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise web.HTTPBadRequest(
                text=json.dumps({"success": False, "error": "invalid json"})
            )
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(
                text=json.dumps({"success": False, "error": "json body must be an object"})
            )
        return payload

    def _is_user_allowed(self, user_id: str) -> bool:
        """检查用户是否通过过滤链。顺序：全局封禁 → 主开关 → 白/黑名单。"""
        settings = self._load_settings()
        filter_cfg = settings.filter

        # 1. 全局封禁始终生效
        if user_id in filter_cfg.ban_user_id:
            return False

        # 2. 主开关关闭时放行所有
        if not filter_cfg.enable_filter:
            return True

        # 3. 白/黑名单策略
        if filter_cfg.user_list_type == "whitelist":
            return user_id in filter_cfg.user_list
        return user_id not in filter_cfg.user_list

    def _check_user_filter(self, payload: Mapping[str, Any]) -> Optional[web.Response]:
        """从 payload 提取 user_id 并检查过滤，返回 403 Response 或 None（放行）。"""
        user_id = str(
            payload.get("user") or payload.get("user_id") or "api-user"
        ).strip() or "api-user"
        if not self._is_user_allowed(user_id):
            settings = self._load_settings()
            if settings.filter.show_dropped_messages:
                self.ctx.logger.info(f"Mai API 过滤拒绝: user_id={user_id}")
            return web.json_response(
                {"success": False, "error": "user not allowed"},
                status=403,
            )
        return None

    @staticmethod
    def _require_text(payload: Mapping[str, Any]) -> str:
        text = str(payload.get("text") or payload.get("message") or "").strip()
        if not text:
            raise web.HTTPBadRequest(text=json.dumps({"success": False, "error": "text is required"}))
        return text

    @staticmethod
    def _normalize_session_id(payload: Mapping[str, Any]) -> str:
        session_id = str(payload.get("session_id") or payload.get("chat_id") or "").strip()
        if session_id:
            return session_id
        # 没有显式 session_id 时，基于 user 标识生成稳定会话
        # MaiBot 内部会话是确定性的(platform+user+account+scope)，API 会话也必须稳定
        # 否则每次请求会覆盖映射，导致回复路由到错误的请求
        user = str(payload.get("user") or payload.get("user_id") or "api-user").strip() or "api-user"
        return f"mai-api-user-{user}"

    @staticmethod
    def _normalize_message_id(payload: Mapping[str, Any]) -> str:
        message_id = str(payload.get("message_id") or "").strip()
        return message_id or f"mai-api:{uuid4().hex}"

    def _normalize_timeout(self, payload: Mapping[str, Any]) -> float:
        try:
            timeout = float(payload.get("timeout") or self._load_settings().server.sync_timeout_sec)
        except (TypeError, ValueError):
            timeout = self._load_settings().server.sync_timeout_sec
        return max(0.5, min(timeout, 120.0))

    def _get_history(self, session_id: str) -> Deque[Dict[str, Any]]:
        history = self._session_messages.get(session_id)
        if history is None:
            maxlen = max(1, int(self._load_settings().server.max_history_per_session))
            history = deque(maxlen=maxlen)
            self._session_messages[session_id] = history
        return history

    async def _wait_for_messages(self, session_id: str, timeout_sec: float) -> None:
        event = self._session_events[session_id]
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.0, min(timeout_sec, 120.0)))
        except asyncio.TimeoutError:
            return

    async def _wait_for_new_messages(
        self,
        session_id: str,
        before_count: int,
        timeout_sec: float,
    ) -> List[Dict[str, Any]]:
        deadline = time.monotonic() + timeout_sec
        # 等待第一条新消息
        while time.monotonic() < deadline:
            history = list(self._get_history(session_id))
            if len(history) > before_count:
                break
            await self._wait_for_messages(session_id, deadline - time.monotonic())
        else:
            return []

        # 收到首条后额外等 2.5s 收集后续分段消息
        grace = min(2.5, deadline - time.monotonic())
        if grace > 0:
            await self._wait_for_messages(session_id, grace)

        return list(self._get_history(session_id))[before_count:]

    async def _remember_session_mapping(self, *, api_session_id: str, internal_user_id: str) -> None:
        internal_session_id = self._calculate_private_session_id(
            platform=DEFAULT_PLATFORM,
            user_id=internal_user_id,
            account_id=DEFAULT_ACCOUNT_ID,
            scope=DEFAULT_SCOPE,
        )
        async with self._lock:
            self._internal_to_api_sessions[internal_session_id] = api_session_id
            self._internal_to_api_sessions[internal_user_id] = api_session_id

    @staticmethod
    def _build_internal_user_id(*, user_id: str, session_id: str) -> str:
        """基于 user_id 生成稳定的用户标识，不依赖 session_id，确保同一用户在不同会话中身份一致。"""
        normalized_user_id = str(user_id or "api-user").strip() or "api-user"
        user_hash = hashlib.sha256(normalized_user_id.encode("utf-8")).hexdigest()[:16]
        return f"{normalized_user_id}#mai-api-{user_hash}"

    @staticmethod
    def _calculate_private_session_id(
        *,
        platform: str,
        user_id: str,
        account_id: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> str:
        components = [platform]
        if account_id:
            components.append(f"account:{account_id}")
        if scope:
            components.append(f"scope:{scope}")
        components.extend([user_id, "private"])
        return hashlib.md5("_".join(components).encode("utf-8")).hexdigest()

    def _session_id_from_message(self, message: Mapping[str, Any]) -> str:
        message_info = message.get("message_info", {})
        additional_config: Mapping[str, Any] = {}
        if isinstance(message_info, Mapping):
            raw_config = message_info.get("additional_config", {})
            if isinstance(raw_config, Mapping):
                additional_config = raw_config

        direct_session_id = str(additional_config.get("mai_api_session_id") or "").strip()
        if direct_session_id:
            return direct_session_id

        for candidate in (message.get("session_id"), additional_config.get("platform_io_target_user_id")):
            normalized_candidate = str(candidate or "").strip()
            if not normalized_candidate:
                continue
            api_session_id = self._internal_to_api_sessions.get(normalized_candidate)
            if api_session_id:
                return api_session_id

        return str(message.get("session_id") or "default").strip()

    @staticmethod
    def _extract_text(message: Mapping[str, Any]) -> str:
        raw_message = message.get("raw_message")
        if not isinstance(raw_message, list):
            return str(message.get("processed_plain_text") or "").strip()

        chunks: List[str] = []
        for segment in raw_message:
            if not isinstance(segment, Mapping):
                continue
            segment_type = str(segment.get("type") or "").strip()
            data = segment.get("data")
            if segment_type == "text":
                if isinstance(data, Mapping):
                    chunks.append(str(data.get("text") or data.get("content") or ""))
                else:
                    chunks.append(str(data or ""))
            elif segment_type == "reply":
                continue
            elif segment_type:
                chunks.append(f"[{segment_type}]")
        return "".join(chunks).strip()

    @staticmethod
    def _extract_last_user_text(messages: List[Any]) -> str:
        for message in reversed(messages):
            if not isinstance(message, Mapping):
                continue
            if str(message.get("role") or "").strip() != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                chunks: List[str] = []
                for part in content:
                    if isinstance(part, Mapping):
                        if part.get("type") == "text":
                            chunks.append(str(part.get("text") or ""))
                    elif isinstance(part, str):
                        chunks.append(part)
                return "".join(chunks).strip()
        return ""

    @staticmethod
    def _extract_system_messages(messages: List[Any]) -> str:
        """从 messages 数组中提取所有 role=system 的消息，拼接为 system prompt。"""
        parts: List[str] = []
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            if str(message.get("role") or "").strip() != "system":
                continue
            content = message.get("content")
            if isinstance(content, str):
                text = content.strip()
                if text:
                    parts.append(text)
            elif isinstance(content, list):
                chunks: List[str] = []
                for part in content:
                    if isinstance(part, Mapping):
                        if part.get("type") == "text":
                            chunks.append(str(part.get("text") or ""))
                    elif isinstance(part, str):
                        chunks.append(part)
                text = "".join(chunks).strip()
                if text:
                    parts.append(text)
        return "\n".join(parts)
