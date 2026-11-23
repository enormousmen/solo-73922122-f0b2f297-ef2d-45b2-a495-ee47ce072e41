"""录像分片环形存储。

模型：
- 固定 slot_count 个槽位，每槽最多一片，分片字节落盘为每槽一个文件。
- 编码器按连续片号上传；写满后新片覆盖"最旧片"所在槽。
- 每次覆盖使槽位代次(generation) +1。游标里带着 (slot, generation, seq, offset)，
  旧游标对不上当前槽位元数据即判为断档，绝不会读到同槽新片的字节。
- 元数据全在内存，由一把 asyncio.Lock 保护；文件读写经 to_thread 挂到锁内，
  因此"校验游标 -> 读字节"与"覆盖写文件 -> 改元数据"互斥，不会读到半新半旧。

录制代号与接替：
- 首次无代号上传创建"初始录制"，服务端生成录制代号(code)并在响应中返回。
- 主编码器故障时，值班员凭当前代号调用 takeover，上传备用编码器的首片
  （必须 independent）。首片先写暂存文件（不持锁，期间原编码器仍可上传、
  终端仍可回看），随后在锁内一次性校验旧代号、选槽、原子改名入库并更换
  当前代号。任何一步失败都不会改元数据、不会淘汰录像。
- 备用首片接在"提交时刻"最后一片已入库的片号之后，回看片号(global seq)
  全局连续；备用编码器后续按自己的本机片号(local seq)连续上传，由所属
  录制代号映射到回看片号。原游标因此可以按原规则跨过接替点。
- 接替生效后，旧代号的任何上传/接替都会被拒绝（校验在锁内、请求体收完之后）。

读路径只在单个请求内短暂持锁（最多读 64 KiB），请求之间不持有任何资源；
写路径不等待任何读者。所以慢终端既拖不住写入，也不会多占磁盘槽。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union


class StoreError(Exception):
    """存储层错误基类。"""


class NonContiguous(StoreError):
    """片号不连续（不是 last_seq + 1）。"""

    def __init__(self, expected: int, got: int):
        super().__init__(f"expected seq {expected}, got {got}")
        self.expected = expected
        self.got = got


class EmptySegment(StoreError):
    """空分片体。"""


class SegmentTooLarge(StoreError):
    """超过单片上限。"""


class BadCursor(StoreError):
    """游标无法解析或自相矛盾。"""


class InvalidCode(StoreError):
    """录制代号不存在或已不是当前代号（例如接替前的主编码器代号）。"""

    def __init__(self, code: str, current_code: Optional[str]):
        super().__init__(f"recording code {code!r} is not current")
        self.code = code
        self.current_code = current_code


class MissingCode(StoreError):
    """初始录制结束后，上传必须携带当前录制代号。"""

    def __init__(self, current_code: str):
        super().__init__("recording code is required after takeover")
        self.current_code = current_code


@dataclass
class SlotMeta:
    seq: int
    independent: bool
    generation: int
    size: int


@dataclass
class Cursor:
    """续读游标：定位到某槽某代某片的某字节偏移。"""

    slot: int
    generation: int
    seq: int
    offset: int

    def encode(self) -> str:
        payload = json.dumps(
            {"s": self.slot, "g": self.generation, "q": self.seq, "o": self.offset},
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(payload).decode()

    @classmethod
    def decode(cls, token: str) -> "Cursor":
        try:
            raw = base64.urlsafe_b64decode(token.encode())
            obj = json.loads(raw)
            cur = cls(
                slot=int(obj["s"]),
                generation=int(obj["g"]),
                seq=int(obj["q"]),
                offset=int(obj["o"]),
            )
        except (binascii.Error, ValueError, KeyError, TypeError, UnicodeEncodeError) as exc:
            raise BadCursor(f"invalid cursor: {exc}") from exc
        if cur.slot < 0 or cur.generation < 0 or cur.seq < 0 or cur.offset < 0:
            raise BadCursor("cursor fields must be non-negative")
        return cur


@dataclass
class Recording:
    """一段录制：一个录制代号及其本机片号 -> 回看片号的映射。"""

    code: str
    ordinal: int  # 1 = 初始录制，2+ = 第几次接替
    first_local_seq: int
    offset: int  # 回看片号 = offset + 本机片号
    last_local_seq: int


# ---- 操作结果 ----


@dataclass
class PutResult:
    seq: int  # 回看片号（全局连续）
    local_seq: int  # 编码器自己的片号
    code: str
    slot: int
    generation: int
    size: int
    independent: bool
    oldest_seq: int
    newest_seq: int


@dataclass
class TakeoverResult:
    code: str  # 新录制代号
    seq: int  # 备用首片的回看片号
    local_seq: int
    slot: int
    generation: int
    size: int
    independent: bool
    oldest_seq: int
    newest_seq: int


@dataclass
class StartOk:
    start_seq: int
    cursor: Cursor


@dataclass
class ReadOk:
    data: bytes
    seq: int
    offset: int
    segment_size: int
    independent: bool
    next_cursor: Cursor


@dataclass
class Gap:
    """断档：所要的片已被覆盖。给出最早仍可起播的片号（可能为 None）。"""

    earliest_playable: Optional[int]


@dataclass
class Wait:
    """等待：暂无可用起点，或下一片尚未写入。"""

    reason: str


StartResult = Union[StartOk, Gap, Wait]
ReadResult = Union[ReadOk, Gap, Wait]


class SegmentStore:
    def __init__(
        self,
        data_dir: str,
        slot_count: int,
        max_segment_bytes: int,
        max_read_bytes: int,
    ):
        if slot_count < 1:
            raise ValueError("slot_count must be >= 1")
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.slot_count = slot_count
        self.max_segment_bytes = max_segment_bytes
        self.max_read_bytes = max_read_bytes
        self._slots: list[Optional[SlotMeta]] = [None] * slot_count
        self._generations: list[int] = [0] * slot_count
        self._index: dict[int, int] = {}  # 回看片号 -> slot
        self._last_seq: Optional[int] = None
        self._current: Optional[Recording] = None
        self._issued_codes: set[str] = set()
        self._lock = asyncio.Lock()
        self._wipe_dir()

    # ---- 文件层（同步，经 to_thread 调用） ----

    def _wipe_dir(self) -> None:
        # 元数据在内存，重启后无法恢复索引，落盘文件一并清空，从头开始。
        for p in self.data_dir.iterdir():
            if p.is_file():
                p.unlink()

    def _slot_path(self, slot: int) -> Path:
        return self.data_dir / f"slot_{slot:04d}.seg"

    def _tmp_path(self, slot: int, generation: int) -> Path:
        return self.data_dir / f"slot_{slot:04d}.g{generation:08d}.tmp"

    def _staging_path(self) -> Path:
        return self.data_dir / f"takeover.{uuid.uuid4().hex}.tmp"

    @staticmethod
    def _write_file(path: Path, data: bytes) -> None:
        with open(path, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())

    def _write_slot_file(self, slot: int, generation: int, data: bytes) -> None:
        tmp = self._tmp_path(slot, generation)
        self._write_file(tmp, data)
        os.replace(tmp, self._slot_path(slot))

    def _read_slot_file(self, slot: int, offset: int, length: int) -> bytes:
        with open(self._slot_path(slot), "rb") as fh:
            fh.seek(offset)
            return fh.read(length)

    # ---- 元数据辅助（须在锁内调用） ----

    def _new_code_locked(self) -> str:
        while True:
            code = secrets.token_urlsafe(18)
            if code not in self._issued_codes:
                self._issued_codes.add(code)
                return code

    def _earliest_playable_locked(self) -> Optional[int]:
        seqs = [m.seq for m in self._slots if m is not None and m.independent]
        return min(seqs) if seqs else None

    def _oldest_seq_locked(self) -> Optional[int]:
        seqs = [m.seq for m in self._slots if m is not None]
        return min(seqs) if seqs else None

    def _pick_slot_locked(self) -> int:
        for i, meta in enumerate(self._slots):
            if meta is None:
                return i
        # 写满了：覆盖最旧片所在槽。
        return min(range(self.slot_count), key=lambda i: self._slots[i].seq)

    def _resolve_upload_recording_locked(self, code: Optional[str]) -> Optional[Recording]:
        """返回本次上传所属录制；首次上传返回 None（稍后创建初始录制）。"""
        if self._current is None:
            if code is not None:
                raise InvalidCode(code, None)
            return None
        if code is None:
            if self._current.ordinal != 1:
                # 无代号上传只适用于初始录制；接替后必须用返回的新代号。
                raise MissingCode(self._current.code)
            return self._current
        if code != self._current.code:
            raise InvalidCode(code, self._current.code)
        return self._current

    def _validate_body(self, data: bytes) -> None:
        if not data:
            raise EmptySegment("empty segment body")
        if len(data) > self.max_segment_bytes:
            raise SegmentTooLarge(
                f"segment is {len(data)} bytes, limit is {self.max_segment_bytes}"
            )

    # ---- 对外操作 ----

    async def put(
        self,
        seq: int,
        independent: bool,
        data: bytes,
        code: Optional[str] = None,
    ) -> PutResult:
        self._validate_body(data)
        async with self._lock:
            rec = self._resolve_upload_recording_locked(code)
            if rec is None:
                # 初始录制：库空时任意片号起步，本机片号即回看片号。
                new_rec = Recording(
                    code=self._new_code_locked(),
                    ordinal=1,
                    first_local_seq=seq,
                    offset=0,
                    last_local_seq=seq,
                )
                global_seq = seq
            else:
                expected = rec.last_local_seq + 1
                if seq != expected:
                    raise NonContiguous(expected=expected, got=seq)
                global_seq = rec.offset + seq
                new_rec = Recording(
                    code=rec.code,
                    ordinal=rec.ordinal,
                    first_local_seq=rec.first_local_seq,
                    offset=rec.offset,
                    last_local_seq=seq,
                )

            slot = self._pick_slot_locked()
            self._generations[slot] += 1
            generation = self._generations[slot]
            await asyncio.to_thread(self._write_slot_file, slot, generation, data)
            old = self._slots[slot]
            if old is not None:
                self._index.pop(old.seq, None)
            self._slots[slot] = SlotMeta(
                seq=global_seq,
                independent=independent,
                generation=generation,
                size=len(data),
            )
            self._index[global_seq] = slot
            self._last_seq = global_seq
            self._current = new_rec
            return PutResult(
                seq=global_seq,
                local_seq=seq,
                code=new_rec.code,
                slot=slot,
                generation=generation,
                size=len(data),
                independent=independent,
                oldest_seq=self._oldest_seq_locked(),
                newest_seq=self._last_seq,
            )

    async def takeover(self, code: str, local_seq: int, data: bytes) -> TakeoverResult:
        """备用首片接替：暂存写入 -> 锁内校验旧代号并原子入库/换代号。"""
        self._validate_body(data)
        staging = self._staging_path()
        # 暂存写入不持锁：期间原编码器仍可上传，终端仍可续读。
        await asyncio.to_thread(self._write_file, staging, data)
        committed = False
        try:
            async with self._lock:
                if self._current is None or code != self._current.code:
                    current = self._current.code if self._current is not None else None
                    raise InvalidCode(code, current)
                # 以提交时刻已入库的最后一片为准，保证回看片号连续。
                if self._last_seq is None:  # 有当前录制就必然有已入库分片
                    raise InvalidCode(code, self._current.code)
                global_seq = self._last_seq + 1
                new_rec = Recording(
                    code=self._new_code_locked(),
                    ordinal=self._current.ordinal + 1,
                    first_local_seq=local_seq,
                    offset=global_seq - local_seq,
                    last_local_seq=local_seq,
                )
                slot = self._pick_slot_locked()
                # 原子改名后立即改元数据；两者都在锁内，读者看不到中间态。
                os.replace(staging, self._slot_path(slot))
                committed = True
                self._generations[slot] += 1
                generation = self._generations[slot]
                old = self._slots[slot]
                if old is not None:
                    self._index.pop(old.seq, None)
                self._slots[slot] = SlotMeta(
                    seq=global_seq,
                    independent=True,
                    generation=generation,
                    size=len(data),
                )
                self._index[global_seq] = slot
                self._last_seq = global_seq
                self._current = new_rec
                return TakeoverResult(
                    code=new_rec.code,
                    seq=global_seq,
                    local_seq=local_seq,
                    slot=slot,
                    generation=generation,
                    size=len(data),
                    independent=True,
                    oldest_seq=self._oldest_seq_locked(),
                    newest_seq=self._last_seq,
                )
        finally:
            if not committed:
                # 接替失败：不占槽、不淘汰任何录像，清掉暂存文件。
                staging.unlink(missing_ok=True)

    async def start(self, seq: int) -> StartResult:
        """从"不晚于 seq、仍留存的最近独立片"起播。"""
        async with self._lock:
            best: Optional[int] = None
            for meta in self._slots:
                if meta is None or not meta.independent or meta.seq > seq:
                    continue
                if best is None or meta.seq > best:
                    best = meta.seq
            if best is not None:
                slot = self._index[best]
                meta = self._slots[slot]
                return StartOk(
                    start_seq=best,
                    cursor=Cursor(slot=slot, generation=meta.generation, seq=best, offset=0),
                )
            earliest = self._earliest_playable_locked()
            if earliest is not None:
                # 还有可起播的片，但都比所求片号新 -> 所求起点已被覆盖。
                return Gap(earliest_playable=earliest)
            # 库里一片可独立解码的都没有 -> 等编码器继续写。
            return Wait(reason="no-independent-start")

    async def read(self, token: str, max_bytes: int) -> ReadResult:
        cursor = Cursor.decode(token)
        if cursor.slot >= self.slot_count:
            raise BadCursor("cursor slot out of range")
        max_bytes = max(1, min(max_bytes, self.max_read_bytes))
        async with self._lock:
            slot = cursor.slot
            generation = cursor.generation
            seq = cursor.seq
            offset = cursor.offset
            while True:
                meta = self._slots[slot]
                if meta is None or meta.seq != seq or meta.generation != generation:
                    # 槽位被覆盖（或重启清空）：旧游标读不到同槽新片。
                    return Gap(earliest_playable=self._earliest_playable_locked())
                if offset > meta.size:
                    raise BadCursor("cursor offset beyond segment size")
                if offset < meta.size:
                    length = min(max_bytes, meta.size - offset)
                    data = await asyncio.to_thread(
                        self._read_slot_file, slot, offset, length
                    )
                    return ReadOk(
                        data=data,
                        seq=seq,
                        offset=offset,
                        segment_size=meta.size,
                        independent=meta.independent,
                        next_cursor=Cursor(
                            slot=slot,
                            generation=generation,
                            seq=seq,
                            offset=offset + length,
                        ),
                    )
                # 本片已读完，顺势滚到下一片。回看片号在接替点前后连续，
                # 因此接替前拿到的游标可以按原规则跨过接替点。
                next_slot = self._index.get(seq + 1)
                if next_slot is None:
                    if self._last_seq is not None and seq + 1 <= self._last_seq:
                        # 片号曾写入过却不在库里了 -> 被覆盖。
                        return Gap(earliest_playable=self._earliest_playable_locked())
                    return Wait(reason="next-segment-not-written")
                slot = next_slot
                seq = seq + 1
                offset = 0
                generation = self._slots[slot].generation

    async def snapshot(self) -> dict:
        async with self._lock:
            rec = self._current
            return {
                "slot_count": self.slot_count,
                "max_segment_bytes": self.max_segment_bytes,
                "max_read_bytes": self.max_read_bytes,
                "code": rec.code if rec is not None else None,
                "recording": (
                    {
                        "code": rec.code,
                        "ordinal": rec.ordinal,
                        "first_local_seq": rec.first_local_seq,
                        "last_local_seq": rec.last_local_seq,
                        "offset": rec.offset,
                    }
                    if rec is not None
                    else None
                ),
                "initial_recording": rec is not None and rec.ordinal == 1,
                "newest_seq": self._last_seq,
                "oldest_seq": self._oldest_seq_locked(),
                "earliest_playable": self._earliest_playable_locked(),
                "slots": [
                    (
                        {
                            "slot": i,
                            "empty": False,
                            "seq": meta.seq,
                            "independent": meta.independent,
                            "generation": meta.generation,
                            "size": meta.size,
                        }
                        if meta is not None
                        else {"slot": i, "empty": True}
                    )
                    for i, meta in enumerate(self._slots)
                ],
            }
