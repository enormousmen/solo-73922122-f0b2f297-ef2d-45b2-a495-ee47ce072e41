"""运行配置。槽位数、数据目录等通过环境变量注入。"""
from __future__ import annotations

import os
from dataclasses import dataclass

# 需求规定的硬指标：单片最大 16 MiB，单次续读最大 64 KiB。
DEFAULT_MAX_SEGMENT_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_READ_BYTES = 64 * 1024


@dataclass(frozen=True)
class Settings:
    slot_count: int = 8
    data_dir: str = "./data"
    max_segment_bytes: int = DEFAULT_MAX_SEGMENT_BYTES
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            slot_count=int(os.environ.get("SLOT_COUNT", "8")),
            data_dir=os.environ.get("DATA_DIR", "./data"),
            max_segment_bytes=int(
                os.environ.get("MAX_SEGMENT_BYTES", str(DEFAULT_MAX_SEGMENT_BYTES))
            ),
            max_read_bytes=int(
                os.environ.get("MAX_READ_BYTES", str(DEFAULT_MAX_READ_BYTES))
            ),
        )
