# segment-recorder：环形覆盖的录像分片接收与回看服务

产线值班员回看录像时，录像盘仍在覆盖旧片。本服务用 FastAPI 接收单路编码器按连续片号上传的录像分片，本地固定槽位环形覆盖保存；终端凭游标按序续读原始字节回看，被覆盖时明确报断档并指引重新起播。

## 功能

- **分片上传**：单路编码器按连续片号上传；单片最大 **16 MiB**；随片用 `independent` 标明能否独立解码。片号不连续返回 409。
- **环形覆盖**：槽位数可配（`SLOT_COUNT`），每槽一片，写满覆盖最旧片；磁盘上始终只有 `SLOT_COUNT` 个分片文件。
- **起播**：从"不晚于所求片号、仍留存的最近独立片"起播。
- **续读**：终端凭游标按序读**原始字节**，每次最多 **64 KiB**（可用 `max_bytes` 调小）；一片读完自动滚入下一片。
- **断档（410）**：找不到起点（起点已被覆盖）或续读位置被覆盖时，返回 JSON 并给出 `earliest_playable`（最早仍可起播的片号）。
- **等待（204）**：库里没有任何可独立解码的片（无可用起点），或已读到直播边缘（下一片尚未写入）。
- **旧游标安全**：游标绑定 `(槽位, 代次, 片号, 偏移)`，覆盖使代次 +1，旧游标永远读不到同槽新片的字节，只会收到 410。
- **慢终端无妨碍**：读路径只在单个请求内短暂持锁（≤64 KiB），请求之间不持有任何资源；写路径不等待读者；槽位固定，慢终端不多占磁盘。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up -d --build     # 监听 8000 端口，SLOT_COUNT=4（便于演示覆盖）
curl localhost:8000/healthz
```

### 本地运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
SLOT_COUNT=4 DATA_DIR=./data .venv/bin/uvicorn app.main:app --port 8000
```

### 端到端演示（含"读到半片被覆盖后重新起播"）

```bash
./demo/demo.sh                   # 自动 docker compose up/down，全程 curl 演示
./demo/demo.sh --no-compose      # 服务已在运行时，直接演（可用 BASE=... 指定地址）
```

演示流程：空库起播(204) → 上传片 1..4 写满 4 槽 → 求片 2 的起点、实际从片 1 起播 →
读完片 1、再读片 2 的一半 → 上传片 5、6 覆盖片 1、2 所在槽 → 旧游标续读收到
**410 断档**及 `earliest_playable=4` → 用该片号**重新起播** → 读到直播边缘(204) →
`cmp` 字节级校验读回内容与上传完全一致。

## API

### `POST /v1/segments/{seq}?independent=true|false`

上传一片。请求体为原始字节（`application/octet-stream`），≤16 MiB，非空。
片号必须连续（等于上一片 +1；库空时可任意起步）。

| 状态码 | 含义 |
|---|---|
| 201 | 已接收，JSON 返回 `{seq, slot, generation, size, independent, oldest_seq, newest_seq}` |
| 400 | 空分片 |
| 409 | 片号不连续，JSON 给出 `expected_seq` / `got_seq` |
| 413 | 超过 16 MiB |

### `GET /v1/playback/start?seq=N`

起播。从"不晚于 N、仍留存的最近独立片"开始。

| 状态码 | 含义 |
|---|---|
| 200 | `{"status":"ok","start_seq":S,"cursor":"..."}`，S 为实际起播片号（≤N） |
| 204 | 等待：库里没有可独立解码的片（`X-Reason: no-independent-start`） |
| 410 | 断档：起点已被覆盖，JSON 给出 `earliest_playable`（最早仍可起播片号，可能为 null） |

### `GET /v1/playback/read?cursor=...&max_bytes=...`

凭游标续读。`max_bytes` 可选，默认且上限 64 KiB。

| 状态码 | 含义 |
|---|---|
| 200 | 响应体即**原始字节**（≤64 KiB）；响应头见下 |
| 204 | 等待：下一片尚未写入（`X-Reason: next-segment-not-written`），同一游标稍后继续有效 |
| 400 | 游标无法解析或越界 |
| 410 | 断档：续读位置已被覆盖，JSON 给出 `earliest_playable`；旧游标不会读到同槽新片 |
| 422 | `max_bytes` 超过 64 KiB |

200 响应头：`X-Next-Cursor`（下一次续读凭据）、`X-Seq`、`X-Offset`、`X-Segment-Size`、
`X-Independent`、`X-End-Of-Segment`。一片读完后下一次请求自动滚入下一片。

### `GET /v1/state` / `GET /healthz`

槽位占用、各槽 `(seq, independent, generation, size)`、最旧/最新片号、最早可起播片号；健康检查。

## 设计要点

- **存储**：每槽一个文件 `slot_XXXX.seg`；写入先落临时文件再 `os.replace` 原子替换；
  元数据（片号、独立标记、代次、大小）全在内存，由一把 `asyncio.Lock` 保护。
- **一致性**："校验游标 → 读字节"与"覆盖写文件 → 改元数据"在同一把锁内互斥，
  不会读到半新半旧；读到的 64 KiB 一旦返回即与当时槽位内容一致。
- **游标**：base64url(JSON `{s:槽位, g:代次, q:片号, o:偏移})`，不透明但无签名（见限制）。
- **断档 vs 等待**：还能救（存在更晚的可起播点）→ 410 并给出最早可起播片号；
  库里根本没东西可播 / 已追到直播边缘 → 204 让客户端稍后再来。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SLOT_COUNT` | 8（compose 中为 4） | 槽位数，每槽一片 |
| `DATA_DIR` | `./data`（镜像中 `/data`） | 分片文件目录 |
| `MAX_SEGMENT_BYTES` | 16777216 | 单片上限，需求规定 16 MiB |
| `MAX_READ_BYTES` | 65536 | 单次续读上限，需求规定 64 KiB |

## 自测

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q     # 18 项用例
```

覆盖：上传校验（连续片号/空片/16 MiB 上限）、环形覆盖顺序与代次递增、起播取
"不晚于所求的最近独立片"、起点被覆盖报 410、空库/无独立片报 204、64 KiB 分块、
跨片自动滚动、直播边缘 204 且游标不失效、半片被覆盖后旧游标 410 且读不到同槽新片、
整圈覆盖后游标失效、端到端字节一致、磁盘槽位不膨胀。

## 限制与未完成

- **重启即清空**：元数据在内存，重启后无法恢复索引，启动时清空数据目录从头开始
  （未做崩溃恢复/索引持久化）。
- **单编码器**：片号连续性按单路设计；多路并发上传会被 409 拒绝。
- **游标无签名**：仅防意外不防伪造，伪造游标只会得到 400/410，不会越权读数据。
- **无鉴权/HTTPS**：假定部署在受信内网。
- **写入串行化**：单把锁串行所有上传与读临界区（单写者下每次写仅阻塞读者一次
  落盘时间），未做分槽锁等进一步优化。
- Docker 镜像/compose 文件已经过静态审查，但本环境无 Docker，未实际构建运行；
  等价路径（同一环境变量、同一入口命令）已在宿主机直接验证。
