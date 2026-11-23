"""录像分片环形存储。

模型：
- 固定 slot_count 个槽位，每槽最多一片，分片字节落盘为每槽一个文件。
- 编码器按连续片号上传；写满后新片覆盖"最旧片"所在槽。
- 每次覆盖使槽位代次(generation) +1。游标里带着 (slot, generation, seq, offset)，
  旧游标对不上当前槽位元数据即判为断档，绝不会读到同槽新片的字节。
- 元数据全在内存，由一把 asyncio.Lock 保护；文件读写经 to_thread 挂到锁内，
  因此"校验游标 -> 读字节"与"覆盖写文件 -> 改元数据"互斥，不会读到半新半旧。

读路径只在单个请求内短暂持锁（最多读 64 KiB），请求之间不持有任何资源；
写路径不等待任何读者。所以慢终端既拖不住写入，也不会多占磁盘槽。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
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


# ---- 操作结果 ----


@dataclass
class PutResult:
    seq: int
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
        self._index: dict[int, int] = {}  # seq -> slot
        self._last_seq: Optional[int] = None
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

    def _write_slot_file(self, slot: int, generation: int, data: bytes) -> None:
        tmp = self._tmp_path(slot, generation)
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._slot_path(slot))

    def _read_slot_file(self, slot: int, offset: int, length: int) -> bytes:
        with open(self._slot_path(slot), "rb") as fh:
            fh.seek(offset)
            return fh.read(length)

    # ---- 元数据辅助（须在锁内调用） ----

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

    # ---- 对外操作 ----

    async def put(self, seq: int, independent: bool, data: bytes) -> PutResult:
        if not data:
            raise EmptySegment("empty segment body")
        if len(data) > self.max_segment_bytes:
            raise SegmentTooLarge(
                f"segment is {len(data)} bytes, limit is {self.max_segment_bytes}"
            )
        async with self._lock:
            if self._last_seq is not None and seq != self._last_seq + 1:
                raise NonContiguous(expected=self._last_seq + 1, got=seq)
            slot = self._pick_slot_locked()
            self._generations[slot] += 1
            generation = self._generations[slot]
            await asyncio.to_thread(self._write_slot_file, slot, generation, data)
            old = self._slots[slot]
            if old is not None:
                self._index.pop(old.seq, None)
            self._slots[slot] = SlotMeta(
                seq=seq, independent=independent, generation=generation, size=len(data)
            )
            self._index[seq] = slot
            self._last_seq = seq
            return PutResult(
                seq=seq,
                slot=slot,
                generation=generation,
                size=len(data),
                independent=independent,
                oldest_seq=self._oldest_seq_locked(),
                newest_seq=self._last_seq,
            )

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
                # 本片已读完，顺势滚到下一片。
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
            return {
                "slot_count": self.slot_count,
                "max_segment_bytes": self.max_segment_bytes,
                "max_read_bytes": self.max_read_bytes,
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
