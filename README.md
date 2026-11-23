# segment-recorder：环形覆盖的录像分片接收与回看服务

产线值班员回看录像时，录像盘仍在覆盖旧片。本服务用 FastAPI 接收单路编码器按连续片号上传的录像分片，本地固定槽位环形覆盖保存；终端凭游标按序续读原始字节回看，被覆盖时明确报断档并指引重新起播。

主编码器故障时，值班员可用 API 提交当前录制代号，让备用编码器以一片"可独立解码的首片"接替；回看流在接替点前后保持连续，终端无需重新起播。

## 功能

- **分片上传**：单路编码器按连续片号上传；单片最大 **16 MiB**；随片用 `independent` 标明能否独立解码。片号不连续返回 409。
- **环形覆盖**：槽位数可配（`SLOT_COUNT`），每槽一片，写满覆盖最旧片；磁盘上始终只有 `SLOT_COUNT` 个分片文件。
- **起播**：从"不晚于所求片号、仍留存的最近独立片"起播。
- **续读**：终端凭游标按序读**原始字节**，每次最多 **64 KiB**（可用 `max_bytes` 调小）；一片读完自动滚入下一片。
- **断档（410）**：找不到起点（起点已被覆盖）或续读位置被覆盖时，返回 JSON 并给出 `earliest_playable`（最早仍可起播的片号）。
- **等待（204）**：库里没有任何可独立解码的片（无可用起点），或已读到直播边缘（下一片尚未写入）。
- **旧游标安全**：游标绑定 `(槽位, 代次, 片号, 偏移)`，覆盖使代次 +1，旧游标永远读不到同槽新片的字节，只会收到 410。
- **慢终端无妨碍**：读路径只在单个请求内短暂持锁（≤64 KiB），请求之间不持有任何资源；写路径不等待读者；槽位固定，慢终端不多占磁盘。
- **录制代号与接替**：首次上传创建初始录制并返回录制代号；备用首片凭当前代号接替，成功后返回新代号。首片写入暂存文件期间原编码器仍可上传、终端仍可回看；入库与换代号原子生效，失败则代号与录像都不变。接替生效后旧代号的任何上传/接替都被拒绝。

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

### 端到端演示

```bash
./demo/demo.sh                   # 环形覆盖 + 读到半片被覆盖后重新起播
./demo/takeover_demo.sh          # 主编码器故障，备用编码器从本机片号 0 接替并跨接替点续读
./demo/demo.sh --no-compose      # 服务已在运行时，直接演（可用 BASE=... 指定地址）
```

`demo.sh` 流程：空库起播(204) → 上传片 1..4 写满 4 槽 → 求片 2 的起点、实际从片 1 起播 →
读完片 1、再读片 2 的一半 → 上传片 5、6 覆盖片 1、2 所在槽 → 旧游标续读收到
**410 断档**及 `earliest_playable=4` → 用该片号**重新起播** → 读到直播边缘(204) →
`cmp` 字节级校验读回内容与上传完全一致。

`takeover_demo.sh` 流程：主编码器不带代号上传片 1、2 并拿到初始代号 → 起播读半片、另读到片 2 末尾 →
**备用首片以本机片号 0、`independent=true` 接替**（回看片号 3）→ 验证旧代号上传/接替被 404、
无代号上传被 409 → 备用编码器凭新代号上传本机片 1（回看片号 4）→
**接替前的两个游标分别跨过接替点**续读到直播边缘 → 状态接口核对当前代号 → `cmp` 字节级校验一致。

## API

### 录制代号（code）

- 首次上传（库空）时不需要代号；服务端创建初始录制并在响应 `code` 字段返回。
- 初始录制期间，后续上传可不带代号，也可显式带同一代号。
- **接替成功后**，后续上传必须带当时返回的新代号；不带代号返回 409。
- 未知或已失效（接替前的主编码器 / 更早的接替）代号返回 **404**，响应里带 `current_code`。

### `POST /v1/segments/{seq}?independent=true|false[&code=...]`

上传一片。`seq` 是**本机片号**（编码器自己的连续片号，接替后从新编码器自己的编号继续）；
请求体为原始字节（`application/octet-stream`），≤16 MiB，非空。
本机片号必须连续（等于所属录制上一次本机片号 +1；初始录制库空时可任意起步）。

响应中的 `seq` 是**回看片号**（全局连续，终端使用），`local_seq` 是请求里的本机片号。

| 状态码 | 含义 |
|---|---|
| 201 | 已接收，JSON 返回 `{code, seq, local_seq, slot, generation, size, independent, oldest_seq, newest_seq}` |
| 400 | 空分片 |
| 404 | 录制代号未知/已失效（`current_code` 给出当前代号） |
| 409 | 本机片号不连续（`expected_seq`/`got_seq`），或接替后未带代号（给出 `current_code`） |
| 413 | 超过 16 MiB |

### `POST /v1/takeover?code=...&local_seq=N&independent=true`

备用编码器首片接替。`code` 为**当前**录制代号；`local_seq` 为备用编码器自己的片号
（演示从 0 开始）；`independent` 必须为 true（首片须可独立解码，否则 400）。
请求体规则与上传相同。

- 首片先写**暂存文件**（此期间原编码器仍可上传、终端仍可回看）；
- 随后在校验锁内确认 `code` 仍是当前代号，选取槽位、原子改名入库，并把备用首片接在
  **提交时刻**最后一片已入库的回看片号之后，同时更换当前代号与映射；
- 失败（代号失效/空片/超限/非独立）时不改任何元数据、不淘汰录像，并清理暂存文件。

| 状态码 | 含义 |
|---|---|
| 201 | 接替生效，JSON 返回 `{code, seq, local_seq, slot, generation, size, independent, oldest_seq, newest_seq}`；`code` 是新代号，`seq` 是首片回看片号 |
| 400 | 空分片，或首片未标独立 |
| 404 | 代号不是当前代号（`current_code` 给出当前代号） |
| 413 | 超过 16 MiB |

### `GET /v1/playback/start?seq=N`

起播。从"不晚于 N、仍留存的最近独立片"开始。接替首片可独立解码，因此也可作为起播点。

| 状态码 | 含义 |
|---|---|
| 200 | `{"status":"ok","start_seq":S,"cursor":"..."}`，S 为实际起播片号（≤N） |
| 204 | 等待：库里没有可独立解码的片（`X-Reason: no-independent-start`） |
| 410 | 断档：起点已被覆盖，JSON 给出 `earliest_playable`（最早仍可起播片号，可能为 null） |

### `GET /v1/playback/read?cursor=...&max_bytes=...`

凭游标续读。`max_bytes` 可选，默认且上限 64 KiB。回看片号在接替点前后连续，
**接替前取得的游标可按原规则跨过接替点**读入备用编码器写入的片。

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

状态：当前录制代号 `code`、`recording`（`ordinal` 第几段录制、本机首/末片号、回看片号偏移）、
`initial_recording`、槽位占用、各槽 `(seq, independent, generation, size)`、最旧/最新片号、
最早可起播片号。健康检查。

### curl 示例：备用编码器从本机片号 0 接替

```bash
B=http://localhost:8000
# 1) 主编码器首次上传（无代号），响应里的 code 即当前录制代号
curl -sS -X POST "$B/v1/segments/1?independent=true" \
     -H 'Content-Type: application/octet-stream' --data-binary @seg_1.bin | jq .
# {"code":"...","seq":1,"local_seq":1,...}
OLD="<上一步的 code>"
curl -sS -X POST "$B/v1/segments/2?independent=false&code=$OLD" \
     -H 'Content-Type: application/octet-stream' --data-binary @seg_2.bin

# 2) 主编码器故障：用当前代号提交备用首片（备用自己的本机片号 0，必须独立）
curl -sS -X POST "$B/v1/takeover?code=$OLD&local_seq=0&independent=true" \
     -H 'Content-Type: application/octet-stream' --data-binary @backup_0.bin | jq .
# {"code":"<新代号>","seq":3,"local_seq":0,...}
NEW="<上一步的 code>"

# 3) 备用编码器按自己的连续片号继续上传
curl -sS -X POST "$B/v1/segments/1?independent=false&code=$NEW" \
     -H 'Content-Type: application/octet-stream' --data-binary @backup_1.bin
# {"seq":4,"local_seq":1,...}

# 4) 旧代号上传 / 接替都被拒；接替后无代号上传被拒
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$B/v1/segments/3?code=$OLD" \
     -H 'Content-Type: application/octet-stream' --data-binary @x.bin     # 404
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$B/v1/takeover?code=$OLD&local_seq=0&independent=true" \
     -H 'Content-Type: application/octet-stream' --data-binary @x.bin     # 404
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$B/v1/segments/3" \
     -H 'Content-Type: application/octet-stream' --data-binary @x.bin     # 409

# 5) 状态可查当前代号；接替前的游标可跨接替点续读
curl -sS "$B/v1/state" | jq '{code, recording}'
curl -sS "$B/v1/playback/start?seq=1" | jq .
# 用返回的 cursor 调 /v1/playback/read，会依次读到片 1、2、3(备用首片)、4(备用本机片 1)
```

## 设计要点

- **存储**：每槽一个文件 `slot_XXXX.seg`；写入先落临时文件再 `os.replace` 原子替换；
  元数据（片号、独立标记、代次、大小）全在内存，由一把 `asyncio.Lock` 保护。
- **录制代号**：每次初始录制/接替生成一个不可猜的代号，内存中记录当前代号；
  上传/接替在锁内、请求体收完之后校验代号，因此接替生效后的旧请求一定被拒。
- **本机片号 -> 回看片号**：每段录制存 `(first_local_seq, offset)`，回看片号 = 本机片号 + offset。
  接替首片的回看片号 = 提交时刻 `last_seq + 1`，备用后续片按本机连续片号自然映射，
  回看流全局连续。
- **暂存接替**：首片写暂存文件不持锁（原编码器可继续写、终端可继续读）；提交阶段在锁内
  原子改名并更新元数据，要么全成要么全不改。
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
.venv/bin/python -m pytest tests/ -q     # 29 项用例
```

原有 18 项覆盖：上传校验（连续片号/空片/16 MiB 上限）、环形覆盖顺序与代次递增、起播取
"不晚于所求的最近独立片"、起点被覆盖报 410、空库/无独立片报 204、64 KiB 分块、
跨片自动滚动、直播边缘 204 且游标不失效、半片被覆盖后旧游标 410 且读不到同槽新片、
整圈覆盖后游标失效、端到端字节一致、磁盘槽位不膨胀。

新增 11 项接替用例覆盖：初始录制返回代号、未知代号 404；备用从本机片号 0 接替后回看片号
连续、原游标跨接替点续读字节一致；片尾游标自动滚入接替首片；接替失败（未知代号/空片/非独立）
不改代号、不淘汰录像、清理暂存；接替生效后旧代号上传/接替 404、无代号上传 409；
多次接替链式映射；暂存写入期间原编码器可上传且终端可回看（并发场景）。

## 限制与未完成

- **重启即清空**：元数据在内存，重启后无法恢复索引，启动时清空数据目录从头开始
  （未做崩溃恢复/索引持久化；录制代号也随之失效）。
- **单写者**：每个录制代号下片号连续性按单路设计；多路并发上传会被 409 拒绝。
- **接替不校验本地片号连续性**：备用首片可用任意 `local_seq`（演示用 0），服务端只要求
  后续本机片号相对首片连续；未限制"必须从 0 起"。
- **游标无签名**：仅防意外不防伪造，伪造游标只会得到 400/410，不会越权读数据。
- **无鉴权/HTTPS**：假定部署在受信内网；录制代号不是访问控制凭据。
- **写入串行化**：单把锁串行所有上传与读临界区（单写者下每次写仅阻塞读者一次
  落盘时间），未做分槽锁等进一步优化。
- Docker 镜像/compose 文件已经过静态审查，但本环境无 Docker，未实际构建运行；
  等价路径（同一环境变量、同一入口命令）已在宿主机直接验证，两个 demo 脚本均已实际跑通。

