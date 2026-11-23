"""自测：环形覆盖、起播规则、游标续读、断档与等待。"""
import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.main import create_app

SLOT_COUNT = 4
SEG = 100 * 1024  # 100 KiB，保证一片要多次 64 KiB 才能读完


@pytest.fixture()
def client(tmp_path):
    app = create_app(Settings(slot_count=SLOT_COUNT, data_dir=str(tmp_path / "data")))
    with TestClient(app) as c:
        yield c


def seg_bytes(seq: int, size: int = SEG) -> bytes:
    """确定性图案：整片都是同一个字节（值=片号），便于校验读到的是哪片。"""
    return bytes([seq % 256]) * size


def upload(client: TestClient, seq: int, independent: bool = False, size: int = SEG):
    return client.post(
        f"/v1/segments/{seq}",
        params={"independent": str(independent).lower()},
        content=seg_bytes(seq, size),
    )


def start(client: TestClient, seq: int):
    return client.get("/v1/playback/start", params={"seq": seq})


def read(client: TestClient, cursor: str, **params):
    return client.get("/v1/playback/read", params={"cursor": cursor, **params})


def start_ok(client: TestClient, seq: int) -> str:
    r = start(client, seq)
    assert r.status_code == 200, r.text
    return r.json()["cursor"]


def read_all(client: TestClient, cursor: str):
    """顺着游标读到非 200 为止，返回 (拼起来的字节, 最后一次响应)。"""
    buf = bytearray()
    while True:
        r = read(client, cursor)
        if r.status_code != 200:
            return bytes(buf), r
        buf += r.content
        cursor = r.headers["x-next-cursor"]


# ---- 上传 ----


def test_upload_and_state(client):
    r = upload(client, 1, independent=True)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["seq"] == 1 and body["slot"] == 0 and body["generation"] == 1
    assert body["size"] == SEG and body["oldest_seq"] == 1 and body["newest_seq"] == 1

    st = client.get("/v1/state").json()
    assert st["slot_count"] == SLOT_COUNT
    assert st["newest_seq"] == 1 and st["oldest_seq"] == 1
    assert st["earliest_playable"] == 1
    assert st["slots"][0]["seq"] == 1 and st["slots"][0]["independent"] is True
    assert all(s["empty"] for s in st["slots"][1:])


def test_upload_requires_continuous_seq(client):
    assert upload(client, 5).status_code == 201  # 空库时任意片号起步
    r = upload(client, 7)
    assert r.status_code == 409
    assert r.json()["expected_seq"] == 6 and r.json()["got_seq"] == 7
    r = upload(client, 5)  # 重发旧片号同样拒绝
    assert r.status_code == 409
    assert upload(client, 6).status_code == 201


def test_upload_rejects_empty(client):
    r = client.post("/v1/segments/1", content=b"")
    assert r.status_code == 400


def test_upload_size_limit(client):
    limit = 16 * 1024 * 1024
    r = client.post("/v1/segments/1", content=seg_bytes(1, limit))
    assert r.status_code == 201
    assert r.json()["size"] == limit
    r = client.post("/v1/segments/2", content=seg_bytes(2, limit + 1))
    assert r.status_code == 413


def test_ring_overwrites_oldest(client):
    for s in range(1, 7):  # 6 片进 4 槽 -> 片 1、2 被覆盖
        assert upload(client, s, independent=(s % 2 == 1)).status_code == 201
    st = client.get("/v1/state").json()
    seqs = sorted(s["seq"] for s in st["slots"])
    assert seqs == [3, 4, 5, 6]
    assert st["oldest_seq"] == 3 and st["newest_seq"] == 6
    # 槽位复用：被覆盖槽的代次已 +1
    gens = {s["slot"]: s["generation"] for s in st["slots"]}
    assert gens == {0: 2, 1: 2, 2: 1, 3: 1}


# ---- 起播 ----


def test_start_picks_nearest_independent_not_later(client):
    for s in range(1, 5):
        upload(client, s, independent=(s in (1, 4)))
    r = start(client, 3)
    assert r.status_code == 200 and r.json()["start_seq"] == 1
    r = start(client, 6)  # 所求片号超过最新片：从最近独立片起播（准直播）
    assert r.status_code == 200 and r.json()["start_seq"] == 4
    r = start(client, 4)
    assert r.status_code == 200 and r.json()["start_seq"] == 4


def test_start_gap_when_start_point_overwritten(client):
    for s in range(1, 7):
        upload(client, s, independent=(s == 4))
    # 留存的独立片只有 4；求 0..3 的起点都已被覆盖
    for q in (0, 1, 2, 3):
        r = start(client, q)
        assert r.status_code == 410, (q, r.text)
        assert r.json()["status"] == "gap"
        assert r.json()["earliest_playable"] == 4


def test_start_wait_when_nothing_playable(client):
    r = start(client, 0)  # 空库
    assert r.status_code == 204 and r.headers["x-reason"] == "no-independent-start"
    upload(client, 1, independent=False)  # 有片但没有独立片
    upload(client, 2, independent=False)
    r = start(client, 2)
    assert r.status_code == 204


# ---- 续读 ----


def test_read_chunks_at_64k_and_wait_at_live_edge(client):
    upload(client, 1, independent=True)
    cursor = start_ok(client, 1)
    sizes = []
    for _ in range(2):  # 100 KiB = 65536 + 36864
        r = read(client, cursor)
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/octet-stream"
        assert int(r.headers["x-seq"]) == 1
        assert int(r.headers["x-segment-size"]) == SEG
        sizes.append(len(r.content))
        cursor = r.headers["x-next-cursor"]
    assert sizes == [65536, SEG - 65536]
    assert max(sizes) <= 64 * 1024
    # 读完最新片 -> 下一片尚未写入 -> 204 等待
    r = read(client, cursor)
    assert r.status_code == 204 and r.headers["x-reason"] == "next-segment-not-written"


def test_read_rolls_across_segments_byte_exact(client):
    upload(client, 1, independent=True, size=64 * 1024)
    upload(client, 2, size=64 * 1024)
    cursor = start_ok(client, 1)
    r1 = read(client, cursor)  # 恰好读完整片 1
    assert r1.status_code == 200 and r1.headers["x-end-of-segment"] == "true"
    r2 = read(client, r1.headers["x-next-cursor"])  # 自动滚入片 2
    assert r2.status_code == 200 and r2.headers["x-seq"] == "2"
    assert r1.content == seg_bytes(1, 64 * 1024)
    assert r2.content == seg_bytes(2, 64 * 1024)


def test_read_max_bytes_param(client):
    upload(client, 1, independent=True)
    cursor = start_ok(client, 1)
    r = read(client, cursor, max_bytes=1000)
    assert r.status_code == 200 and len(r.content) == 1000
    r = read(client, cursor, max_bytes=64 * 1024 + 1)  # 超过 64 KiB 上限
    assert r.status_code == 422


def test_bad_cursor_rejected(client):
    upload(client, 1, independent=True)
    assert read(client, "not-a-cursor").status_code == 400
    # 合法 base64/JSON 但槽位越界
    assert read(client, "eyJzIjo5OSwiZyI6MSwicSI6MSwibyI6MH0=").status_code == 400


def test_wait_does_not_invalidate_cursor(client):
    upload(client, 1, independent=True, size=1024)
    cursor = start_ok(client, 1)
    r = read(client, cursor)
    assert r.status_code == 200
    cursor = r.headers["x-next-cursor"]
    assert read(client, cursor).status_code == 204  # 直播边缘，等待
    upload(client, 2, size=1024)  # 下一片写入了
    r = read(client, cursor)  # 同一游标继续，自动滚入片 2
    assert r.status_code == 200
    assert r.headers["x-seq"] == "2" and r.content == seg_bytes(2, 1024)


# ---- 断档与重新起播（核心场景） ----


def test_gap_when_overwritten_mid_segment_then_restart(client):
    for s in range(1, 5):
        upload(client, s, independent=(s == 1))
    cursor = start_ok(client, 1)
    r = read(client, cursor)  # 片 1 读到 64 KiB 处（共 100 KiB）
    assert r.status_code == 200
    half_cursor = r.headers["x-next-cursor"]

    # 编码器继续写：片 5 覆盖片 1 所在槽（此时库里已没有独立片）
    assert upload(client, 5).status_code == 201
    assert client.get("/v1/state").json()["oldest_seq"] == 2

    # 旧游标续读：必须报断档，绝不允许返回同槽新片（片 5）的字节
    r = read(client, half_cursor)
    assert r.status_code == 410
    assert r.json()["status"] == "gap"
    assert r.json()["earliest_playable"] is None  # 独立片 1 已被覆盖

    # 写入新的独立片 6 后，断档响应给出最早仍可起播的片号
    assert upload(client, 6, independent=True).status_code == 201
    r = read(client, half_cursor)
    assert r.status_code == 410 and r.json()["earliest_playable"] == 6

    # 重新起播并读到直播边缘，字节与上传完全一致
    cursor = start_ok(client, 6)
    data, last = read_all(client, cursor)
    assert last.status_code == 204
    assert data == seg_bytes(6)


def test_old_cursor_never_reads_new_segment_in_same_slot(client):
    upload(client, 1, independent=True, size=64 * 1024)
    cursor = start_ok(client, 1)
    r = read(client, cursor)  # 读完整片 1，游标停在片尾
    end_cursor = r.headers["x-next-cursor"]
    # 片 2..5 写入，片 1 的槽被片 5 覆盖
    for s in range(2, 6):
        upload(client, s)
    r = read(client, end_cursor)
    assert r.status_code == 410  # 而不是返回片 5 的字节


def test_full_rotation_invalidates_cursor(client):
    upload(client, 1, independent=True, size=1024)
    cursor = start_ok(client, 1)
    for s in range(2, 7):  # 整圈覆盖
        upload(client, s, independent=(s == 5))
    r = read(client, cursor)
    assert r.status_code == 410
    assert r.json()["earliest_playable"] == 5


def test_end_to_end_byte_exact(client):
    for s in range(1, 7):
        upload(client, s, independent=(s in (1, 4)))
    cursor = start_ok(client, 6)  # 起播点应为片 4
    data, last = read_all(client, cursor)
    assert last.status_code == 204
    assert data == seg_bytes(4) + seg_bytes(5) + seg_bytes(6)


def test_disk_slots_do_not_grow(client, tmp_path):
    """慢终端/频繁读写都不会让磁盘槽位变多：数据目录里始终只有 slot_count 个分片文件。"""
    for s in range(1, 8):
        upload(client, s, independent=(s == 5))
    cursor = start_ok(client, 7)
    read(client, cursor)
    files = list((tmp_path / "data").iterdir())
    assert len(files) == SLOT_COUNT
    assert sorted(p.name for p in files) == [
        f"slot_{i:04d}.seg" for i in range(SLOT_COUNT)
    ]
