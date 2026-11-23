"""自测：备用编码器接替（录制代号、暂存提交、跨接替点续读）。"""
import asyncio
import threading

import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import SegmentStore

SLOT_COUNT = 4
SEG = 100 * 1024


@pytest.fixture()
def app_settings(tmp_path):
    return Settings(slot_count=SLOT_COUNT, data_dir=str(tmp_path / "data"))


@pytest.fixture()
def client(app_settings):
    app = create_app(app_settings)
    with TestClient(app) as c:
        yield c


def payload(value: int, size: int = SEG) -> bytes:
    return bytes([value % 256]) * size


def upload(client, seq, independent=False, size=SEG, code=None, value=None):
    params = {"independent": str(independent).lower()}
    if code is not None:
        params["code"] = code
    return client.post(
        f"/v1/segments/{seq}",
        params=params,
        content=payload(seq if value is None else value, size),
    )


def takeover(client, code, local_seq, independent=True, size=SEG, value=None):
    value = local_seq if value is None else value
    return client.post(
        "/v1/takeover",
        params={
            "code": code,
            "local_seq": local_seq,
            "independent": str(independent).lower(),
        },
        content=payload(value, size),
    )


def state(client):
    return client.get("/v1/state").json()


def start(client, seq):
    return client.get("/v1/playback/start", params={"seq": seq})


def read(client, cursor, **params):
    return client.get("/v1/playback/read", params={"cursor": cursor, **params})


def read_all(client, cursor):
    buf = bytearray()
    while True:
        r = read(client, cursor)
        if r.status_code != 200:
            return bytes(buf), r
        buf += r.content
        cursor = r.headers["x-next-cursor"]


# ---- 初始录制与代号 ----


def test_initial_upload_returns_code_and_no_code_needed(client):
    r = upload(client, 1, independent=True)
    assert r.status_code == 201
    body = r.json()
    assert body["local_seq"] == 1 and body["seq"] == 1
    code = body["code"]
    assert code
    # 初始录制的后续上传仍可不带代号，也可显式带同一代号。
    assert upload(client, 2).status_code == 201
    r = upload(client, 3, code=code)
    assert r.status_code == 201 and r.json()["code"] == code
    st = state(client)
    assert st["code"] == code
    assert st["initial_recording"] is True
    assert st["recording"]["ordinal"] == 1
    assert st["recording"]["offset"] == 0


def test_unknown_code_on_empty_store_rejected(client):
    r = upload(client, 1, code="nope")
    assert r.status_code == 404
    assert state(client)["code"] is None


# ---- 接替主流程 ----


def test_takeover_from_local_seq_zero_then_continue_and_cross_point(client):
    # 主编码器：回看片号 1、2。
    c1 = upload(client, 1, independent=True).json()["code"]
    assert upload(client, 2, independent=True).status_code == 201
    # 接替前拿到的游标（片 1 仍在库中，4 槽放得下接替首片 + 1 片）。
    cur = start(client, 1).json()["cursor"]

    # 值班员提交当前代号 + 备用首片（本机片号 0，必须可独立解码）。
    r = takeover(client, c1, 0, value=200)
    assert r.status_code == 201, r.text
    body = r.json()
    new_code = body["code"]
    assert new_code != c1
    assert body["seq"] == 3  # 接在最后一片已入库的片 2 之后
    assert body["local_seq"] == 0
    assert body["independent"] is True

    st = state(client)
    assert st["code"] == new_code
    assert st["newest_seq"] == 3
    assert st["recording"] == {
        "code": new_code,
        "ordinal": 2,
        "first_local_seq": 0,
        "last_local_seq": 0,
        "offset": 3,  # 回看片号 = 3 + 本机片号
    }

    # 备用编码器凭新代号按自己的连续片号继续上传：本机 1 -> 回看 4。
    r = upload(client, 1, code=new_code, value=201)
    assert r.status_code == 201 and r.json()["seq"] == 4

    # 原游标（接替前从片 1 起播）按原规则跨过接替点读到备用片。
    data, last = read_all(client, cur)
    assert last.status_code == 204  # 读到直播边缘
    assert data == payload(1) + payload(2) + payload(200) + payload(201)


def test_cursor_at_end_of_last_pre_takeover_segment_rolls_into_takeover(client):
    code = upload(client, 1, independent=True, size=64 * 1024).json()["code"]
    cur = start(client, 1).json()["cursor"]
    r = read(client, cur)  # 恰读完整片 1
    assert r.status_code == 200 and r.headers["x-end-of-segment"] == "true"
    end_cur = r.headers["x-next-cursor"]
    # 接替前：直播边缘等待。
    assert read(client, end_cur).status_code == 204
    assert takeover(client, code, 0, size=64 * 1024, value=77).status_code == 201
    # 接替后：同一游标自动滚入备用首片（回看片号 2）。
    r = read(client, end_cur)
    assert r.status_code == 200
    assert r.headers["x-seq"] == "2"
    assert r.content == payload(77, 64 * 1024)


def test_takeover_failure_keeps_current_code_and_recordings(client):
    code = upload(client, 1, independent=True).json()["code"]
    for s in (2, 3, 4):
        upload(client, s)  # 4 槽写满，接替失败不得淘汰任何一片
    before = state(client)

    r = takeover(client, "stale-code", 0)
    assert r.status_code == 404 and r.json()["current_code"] == code
    after = state(client)
    assert after == before  # 代号、槽位、代次、片号全都不变

    # 暂存文件已清理，旧代号仍可正常上传。
    assert upload(client, 5, code=code).status_code == 201
    assert state(client)["code"] == code


def test_takeover_rejects_non_independent_first_segment(client):
    code = upload(client, 1, independent=True).json()["code"]
    before = state(client)
    r = takeover(client, code, 0, independent=False)
    assert r.status_code == 400
    assert state(client) == before


def test_takeover_empty_body_rejected_without_side_effects(client):
    code = upload(client, 1, independent=True).json()["code"]
    before = state(client)
    r = client.post("/v1/takeover", params={"code": code, "local_seq": 0, "independent": "true"}, content=b"")
    assert r.status_code == 400
    assert state(client) == before


def test_takeover_on_empty_store_rejected(client):
    r = takeover(client, "whatever", 0)
    assert r.status_code == 404
    assert r.json()["current_code"] is None
    assert state(client)["code"] is None


def test_takeover_first_segment_is_playable_start(client):
    c1 = upload(client, 1, independent=True).json()["code"]
    upload(client, 2, independent=False)
    new = takeover(client, c1, 0, value=42).json()["code"]
    # 接替首片可独立解码，可作为起播点。
    r = start(client, 3)
    assert r.status_code == 200 and r.json()["start_seq"] == 3
    data, last = read_all(client, r.json()["cursor"])
    assert last.status_code == 204
    assert data == payload(42)


def test_takeover_oversize_body_rejected_without_side_effects(client):
    c1 = upload(client, 1, independent=True).json()["code"]
    before = state(client)
    limit = 16 * 1024 * 1024
    r = client.post(
        "/v1/takeover",
        params={"code": c1, "local_seq": 0, "independent": "true"},
        content=payload(0, limit + 1),
    )
    assert r.status_code == 413
    assert state(client) == before


# ---- 接替生效后的旧代号拒绝 ----


def test_old_code_uploads_and_takeovers_rejected_after_takeover(client):
    old = upload(client, 1, independent=True).json()["code"]
    upload(client, 2)
    r = takeover(client, old, 0, value=90)
    assert r.status_code == 201
    new = r.json()["code"]

    # 请求体完整收齐后仍按代号拒绝旧上传。
    r = upload(client, 3, code=old)
    assert r.status_code == 404 and r.json()["current_code"] == new
    # 旧代号再次接替也拒绝，且不产生新代次。
    before = state(client)
    r = takeover(client, old, 0)
    assert r.status_code == 404
    assert state(client) == before
    # 无代号上传在接替后不再适用。
    r = upload(client, 3)
    assert r.status_code == 409 and r.json()["current_code"] == new
    # 只有当前代号可以继续。
    assert upload(client, 1, code=new).status_code == 201


def test_takeover_after_takeover_chains_global_seq(client):
    c1 = upload(client, 1, independent=True).json()["code"]
    c2 = takeover(client, c1, 0, value=10).json()["code"]
    assert upload(client, 1, code=c2, value=11).json()["seq"] == 3
    c3 = takeover(client, c2, 5, value=12).json()["code"]
    assert c3 != c2
    # 第三次录制的本机片号从 5 起，接在回看片号 3 之后。
    assert upload(client, 6, code=c3, value=13).json()["seq"] == 5
    st = state(client)
    assert st["recording"]["ordinal"] == 3
    assert st["recording"]["offset"] == -1  # 4 - 5


# ---- 暂存期间并发：原编码器仍可上传、终端仍可回看 ----


def test_staging_does_not_block_uploads_or_reads(tmp_path, monkeypatch):
    store = SegmentStore(
        data_dir=str(tmp_path / "data"),
        slot_count=SLOT_COUNT,
        max_segment_bytes=16 * 1024 * 1024,
        max_read_bytes=64 * 1024,
    )

    async def scenario():
        first = await store.put(1, True, payload(1))
        await store.put(2, True, payload(2))
        cursor = (await store.start(1)).cursor.encode()

        release = threading.Event()
        staging_started = threading.Event()
        original = SegmentStore._write_file

        def slow_write(path, data):
            if path.name.startswith("takeover."):
                staging_started.set()
                release.wait(timeout=10)
            original(path, data)

        monkeypatch.setattr(SegmentStore, "_write_file", staticmethod(slow_write))

        takeover_task = asyncio.create_task(
            store.takeover(first.code, 0, payload(200))
        )
        await asyncio.to_thread(staging_started.wait, 5)

        # 暂存文件写入期间：原编码器仍可提交分片，终端仍可回看。
        put = await asyncio.wait_for(store.put(3, False, payload(3)), timeout=5)
        assert put.seq == 3
        read = await asyncio.wait_for(store.read(cursor, 64 * 1024), timeout=5)
        assert read.data == payload(1)[: 64 * 1024]

        release.set()
        result = await asyncio.wait_for(takeover_task, timeout=5)
        # 首片接在暂存期间最后入库的片 3 之后。
        assert result.seq == 4
        assert (await store.read(cursor, 64 * 1024)).data == payload(1)[: 64 * 1024]

    asyncio.run(scenario())
