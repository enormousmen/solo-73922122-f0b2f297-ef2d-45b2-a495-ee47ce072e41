#!/usr/bin/env bash
# 端到端演示：环形覆盖 + 读到半片被覆盖后的断档与重新起播。
#
# 用法：
#   ./demo/demo.sh                  # 用 docker compose 起服务（SLOT_COUNT=4）
#   ./demo/demo.sh --no-compose     # 服务已在运行，直接演（配合 --base）
#   BASE=http://127.0.0.1:8000 ./demo/demo.sh --no-compose
#
# 前提：服务以 SLOT_COUNT=4 运行（compose 文件里已默认）；需要 curl 和 python3。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
COMPOSE=1
KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-compose) COMPOSE=0; shift ;;
    --keep)       KEEP=1; shift ;;
    --base)       BASE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n== %s ==\n' "$*"; }

WORK="$(mktemp -d)"
cleanup() {
  rm -rf "$WORK"
  if [[ "$COMPOSE" == 1 && "$KEEP" == 0 ]]; then
    say "收尾: docker compose down"
    (cd "$(dirname "$0")/.." && docker compose down)
  fi
}
trap cleanup EXIT

if [[ "$COMPOSE" == 1 ]]; then
  say "启动服务 (docker compose up -d --build)"
  (cd "$(dirname "$0")/.." && docker compose up -d --build)
fi

say "等待服务就绪 $BASE"
ready=0
for _ in $(seq 1 60); do
  if curl -fsS "$BASE/healthz" >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
[[ "$ready" == 1 ]] || { echo "服务未就绪"; exit 1; }
echo "  /healthz OK"

json_field() { python3 -c 'import sys,json;print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

# 生成 100 KiB 的分片文件：整片都是同一个字节（值=片号），方便肉眼和脚本核对。
make_seg() { python3 -c "import sys;sys.stdout.buffer.write(bytes([$1 % 256])*102400)" > "$WORK/seg_$1.bin"; }

upload() { # <seq> <independent>
  local code
  code=$(curl -sS -o /dev/null -w '%{http_code}' \
    -H 'Content-Type: application/octet-stream' \
    --data-binary @"$WORK/seg_$1.bin" \
    "$BASE/v1/segments/$1?independent=$2")
  echo "  POST /v1/segments/$1?independent=$2 -> $code"
}

CURSOR=""; START_SEQ=""; READ_CODE=""; XSEQ=""
start_playback() { # <seq>
  local code
  code=$(curl -sS -o "$WORK/start_body" -w '%{http_code}' "$BASE/v1/playback/start?seq=$1")
  echo "  GET /v1/playback/start?seq=$1 -> $code $(cat "$WORK/start_body")"
  if [[ "$code" == 200 ]]; then
    CURSOR=$(json_field cursor < "$WORK/start_body")
    START_SEQ=$(json_field start_seq < "$WORK/start_body")
  fi
}

read_next() { # <outfile>  —— 用全局 CURSOR，成功后推进 CURSOR
  local code
  code=$(curl -sS -D "$WORK/headers" -o "$1" -w '%{http_code}' \
    "$BASE/v1/playback/read?cursor=$CURSOR")
  READ_CODE=$code
  XSEQ=""
  if [[ "$code" == 200 ]]; then
    CURSOR=$(sed -n 's/^[Xx]-[Nn]ext-[Cc]ursor: //p' "$WORK/headers" | tr -d '\r')
    XSEQ=$(sed -n 's/^[Xx]-[Ss]eq: //p' "$WORK/headers" | tr -d '\r')
  fi
}

show_state() {
  curl -sS "$BASE/v1/state" | python3 -c '
import sys, json
st = json.load(sys.stdin)
parts = []
for s in st["slots"]:
    if s["empty"]:
        parts.append("-")
    else:
        mark = "I" if s["independent"] else " "
        parts.append("slot%d:seq%d%s(g%d)" % (s["slot"], s["seq"], mark, s["generation"]))
print("  slots: " + ", ".join(parts))
print("  oldest=%s newest=%s earliest_playable=%s" % (st["oldest_seq"], st["newest_seq"], st["earliest_playable"]))'
}

say "0. 初始状态"
show_state

say "1. 空库起播 -> 204 等待（无可用起点）"
code=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/v1/playback/start?seq=0")
echo "  GET /v1/playback/start?seq=0 -> $code (204=等待)"

say "2. 上传片 1..4（每片 100 KiB；片 1、4 可独立解码），4 个槽写满"
for s in 1 2 3 4; do make_seg "$s"; done
upload 1 true; upload 2 false; upload 3 false; upload 4 true
show_state

say "3. 请求从片 2 起播 -> 实际从不晚于 2 的最近独立片（片 1）起播"
start_playback 2
echo "  实际起播片号 start_seq=$START_SEQ"

say "4. 顺序续读：读完片 1（两次 64K/36K），再读片 2 的前 64 KiB"
read_next "$WORK/c1"; echo "  read -> $READ_CODE 片$XSEQ $(stat -c%s "$WORK/c1") 字节"
read_next "$WORK/c2"; echo "  read -> $READ_CODE 片$XSEQ $(stat -c%s "$WORK/c2") 字节"
read_next "$WORK/c3"; echo "  read -> $READ_CODE 片$XSEQ $(stat -c%s "$WORK/c3") 字节（片 2 只读了一半）"

say "5. 编码器继续上传片 5、6 -> 覆盖片 1、2 所在槽"
make_seg 5; make_seg 6
upload 5 false; upload 6 false
show_state

say "6. 旧游标续读 -> 410 断档（不会读到同槽新片的字节），给出最早仍可起播片号"
read_next "$WORK/gap1"
echo "  read -> $READ_CODE $(cat "$WORK/gap1")"

say "6b. 再次请求从片 2 起播 -> 410 断档（起点已被覆盖）"
start_playback 2

say "7. 用最早仍可起播的片号重新起播"
EP=$(json_field earliest_playable < "$WORK/gap1")
echo "  earliest_playable=$EP"
start_playback "$EP"
echo "  实际起播片号 start_seq=$START_SEQ"

say "8. 从新起点一直读到直播边缘 -> 末尾 204 等待（下一片尚未写入）"
i=0
while true; do
  i=$((i+1))
  read_next "$WORK/r$i"
  size=$(stat -c%s "$WORK/r$i")
  echo "  read#$i -> $READ_CODE 片${XSEQ:-?} $size 字节"
  [[ "$READ_CODE" == 200 ]] || break
  [[ $i -lt 20 ]] || { echo "  异常：读的次数过多"; exit 1; }
done

say "9. 字节级校验"
cat "$WORK/c1" "$WORK/c2" > "$WORK/got_seg1"
cmp "$WORK/seg_1.bin" "$WORK/got_seg1" && echo "  OK: 片 1 读回字节与上传完全一致"
head -c 65536 "$WORK/seg_2.bin" > "$WORK/exp_seg2_half"
cmp "$WORK/exp_seg2_half" "$WORK/c3" && echo "  OK: 片 2 前半（被覆盖前读到的部分）一致"
cat "$WORK/seg_4.bin" "$WORK/seg_5.bin" "$WORK/seg_6.bin" > "$WORK/exp_restart"
# 重新起播后读到的全部内容（r1..r$i，最后一次 204 是空文件，不影响拼接）
cat "$WORK"/r* > "$WORK/got_restart"
cmp "$WORK/exp_restart" "$WORK/got_restart" && echo "  OK: 重新起播后读到的 == 片4+片5+片6 原始字节"

say "演示完成"
