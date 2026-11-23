"""FastAPI 入口：录像分片接收 + 游标续读回看 + 录制代号接替。

接口一览：
  POST /v1/segments/{seq}?independent=true|false[&code=...]  上传一片（原始字节，<=16 MiB）
  POST /v1/takeover?code=...&local_seq=N                    备用编码器首片接替
  GET  /v1/playback/start?seq=N                              起播，返回游标
  GET  /v1/playback/read?cursor=...&max_bytes=...            凭游标续读，每次 <=64 KiB 原始字节
  GET  /v1/state                                             槽位/片号/录制代号现状
  GET  /healthz                                              健康检查

状态语义：
  200 正常（续读时响应体即原始字节，下一游标在 X-Next-Cursor 响应头）
  201 上传/接替成功
  204 等待（无可用起点 / 下一片尚未写入，原因在 X-Reason 响应头）
  404 录制代号未知
  409 片号不连续 / 接替前未显式确认（见接口说明）
  410 断档（起点或续读位置已被覆盖，JSON 里给出 earliest_playable）
  413 超过 16 MiB
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Path, Query, Request, Response
from fastapi.responses import JSONResponse

from .config import Settings
from .store import (
    BadCursor,
    EmptySegment,
    Gap,
    InvalidCode,
    MissingCode,
    NonContiguous,
    ReadOk,
    SegmentStore,
    SegmentTooLarge,
    StartOk,
)

REASON_NO_START = "no-independent-start"
REASON_NO_NEXT = "next-segment-not-written"


def create_app(settings: Settings) -> FastAPI:
    store = SegmentStore(
        data_dir=settings.data_dir,
        slot_count=settings.slot_count,
        max_segment_bytes=settings.max_segment_bytes,
        max_read_bytes=settings.max_read_bytes,
    )
    app = FastAPI(title="segment-recorder", version="0.2.0")
    app.state.store = store

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    async def _read_body(request: Request) -> Optional[Response]:
        """读请求体；超限返回 413 响应，否则返回 None。"""
        # 先信 Content-Length 快速拒绝，再边读边限长，避免超限片占内存。
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit():
            if int(declared) > store.max_segment_bytes:
                return _too_large(store.max_segment_bytes)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > store.max_segment_bytes:
                return _too_large(store.max_segment_bytes)
        request.state.body = bytes(body)
        return None

    def _code_error(exc: InvalidCode) -> JSONResponse:
        # 旧代号（接替前的主编码器 / 已接替过的代号）一律拒绝。
        return JSONResponse(
            status_code=404,
            content={
                "detail": str(exc),
                "code": exc.code,
                "current_code": exc.current_code,
            },
        )

    @app.post("/v1/segments/{seq}", status_code=201)
    async def upload_segment(
        request: Request,
        seq: int = Path(ge=0),
        independent: bool = Query(False),
        code: Optional[str] = Query(None),
    ):
        too_large = await _read_body(request)
        if too_large is not None:
            return too_large
        try:
            result = await store.put(
                seq=seq, independent=independent, data=request.state.body, code=code
            )
        except EmptySegment:
            return JSONResponse(status_code=400, content={"detail": "empty segment body"})
        except NonContiguous as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": str(exc),
                    "expected_seq": exc.expected,
                    "got_seq": exc.got,
                },
            )
        except MissingCode as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "recording code is required: current recording is a takeover",
                    "current_code": exc.current_code,
                },
            )
        except InvalidCode as exc:
            return _code_error(exc)
        return {
            "code": result.code,
            "seq": result.seq,
            "local_seq": result.local_seq,
            "slot": result.slot,
            "generation": result.generation,
            "size": result.size,
            "independent": result.independent,
            "oldest_seq": result.oldest_seq,
            "newest_seq": result.newest_seq,
        }

    @app.post("/v1/takeover", status_code=201)
    async def takeover(
        request: Request,
        code: str = Query(...),
        local_seq: int = Query(ge=0),
        independent: bool = Query(False),
    ):
        """备用编码器首片接替：必须 independent；成功后返回新录制代号。

        请求体收完之后才校验代号与提交（旧代号在接替生效后会被拒绝）。
        """
        too_large = await _read_body(request)
        if too_large is not None:
            return too_large
        if not independent:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "takeover requires an independently decodable first segment"
                },
            )
        try:
            result = await store.takeover(
                code=code, local_seq=local_seq, data=request.state.body
            )
        except EmptySegment:
            return JSONResponse(status_code=400, content={"detail": "empty segment body"})
        except InvalidCode as exc:
            return _code_error(exc)
        return {
            "code": result.code,
            "seq": result.seq,
            "local_seq": result.local_seq,
            "slot": result.slot,
            "generation": result.generation,
            "size": result.size,
            "independent": result.independent,
            "oldest_seq": result.oldest_seq,
            "newest_seq": result.newest_seq,
        }

    @app.get("/v1/playback/start")
    async def playback_start(seq: int = Query(ge=0)):
        result = await store.start(seq)
        if isinstance(result, StartOk):
            return {
                "status": "ok",
                "start_seq": result.start_seq,
                "cursor": result.cursor.encode(),
            }
        if isinstance(result, Gap):
            return JSONResponse(
                status_code=410,
                content={
                    "status": "gap",
                    "detail": "requested start point has been overwritten",
                    "earliest_playable": result.earliest_playable,
                },
            )
        return Response(status_code=204, headers={"X-Reason": REASON_NO_START})

    @app.get("/v1/playback/read")
    async def playback_read(
        cursor: str = Query(...),
        max_bytes: int = Query(
            default=settings.max_read_bytes, ge=1, le=settings.max_read_bytes
        ),
    ):
        try:
            result = await store.read(cursor, max_bytes)
        except BadCursor as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})
        if isinstance(result, ReadOk):
            end_offset = result.offset + len(result.data)
            headers = {
                "X-Next-Cursor": result.next_cursor.encode(),
                "X-Seq": str(result.seq),
                "X-Offset": str(result.offset),
                "X-Segment-Size": str(result.segment_size),
                "X-Independent": "true" if result.independent else "false",
                "X-End-Of-Segment": "true" if end_offset >= result.segment_size else "false",
            }
            return Response(
                content=result.data,
                media_type="application/octet-stream",
                headers=headers,
            )
        if isinstance(result, Gap):
            return JSONResponse(
                status_code=410,
                content={
                    "status": "gap",
                    "detail": "segment was overwritten before it could be read",
                    "earliest_playable": result.earliest_playable,
                },
            )
        return Response(status_code=204, headers={"X-Reason": REASON_NO_NEXT})

    @app.get("/v1/state")
    async def state():
        return await store.snapshot()

    @app.get("/")
    async def root():
        return {
            "service": "segment-recorder",
            "endpoints": [
                "POST /v1/segments/{seq}?independent=&code=",
                "POST /v1/takeover?code=&local_seq=",
                "GET /v1/playback/start?seq=",
                "GET /v1/playback/read?cursor=&max_bytes=",
                "GET /v1/state",
                "GET /healthz",
            ],
        }

    return app


def _too_large(limit: int) -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={"detail": f"segment exceeds {limit} bytes (16 MiB) limit"},
    )


# uvicorn app.main:app 使用的模块级实例（测试走 create_app 自定义配置）。
app = create_app(Settings.from_env())
