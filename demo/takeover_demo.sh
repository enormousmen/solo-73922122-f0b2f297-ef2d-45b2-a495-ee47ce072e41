#!/usr/bin/env bash
# 端到端演示：主编码器故障，值班员用 API 让备用编码器从本机片号 0 接替，
# 原游标跨过接替点继续回看。
#
# 用法：
#   ./demo/takeover_demo.sh                  # 用 docker compose 起服务（SLOT_COUNT=4）
#   ./demo/takeover_demo.sh --no-compose     # 服务已在运行，直接演（配合 --base）
#   BASE=http://127.0.0.1:8000 ./demo/takeover_demo.sh --no-compose
#
# 前提：服务以 SLOT_COUNT=4 运行（compose 默认）；需要 curl 与 python3。
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
# 生成 100 KiB 分片：整片同字节，图案值用于区分不同来源的片。
make_seg() { python3 -c "import sys;sys.stdout.buffer.write(bytes([$1 % 256])*102400)" > "$2"; }

# 主编码器上传：<本机片号> <independent> <code|-> <文件>；成功时把返回代号写到 $WORK/code
upload() { # <seq> <independent> <code|-> <file>
  local url="$BASE/v1/segments/$1?independent=$2"
  [[ "$3" != "-" ]] && url="$url&code=$3"
  local code
  code=$(curl -sS -o "$WORK/upload_body" -w '%{http_code}' \
    -H 'Content-Type: application/octet-stream' \
    --data-binary @"$4" "$url")
  if [[ "$code" == 201 ]]; then
    json_field code < "$WORK/upload_body" > "$WORK/code"
    json_field seq  < "$WORK/upload_body" > "$WORK/seq"
    echo "  POST /v1/segments/$1 (independent=$2, code=$3) -> 201 回看片号=$(cat "$WORK/seq")"
  else
    echo "  POST /v1/segments/$1 (independent=$2, code=$3) -> $code $(cat "$WORK/upload_body")"
  fi
  UPLOAD_CODE=$code
}

# 备用首片接替：<旧代号> <备用本机片号> <文件>；把新代号写到 $WORK/new_code
takeover() { # <code> <local_seq> <file>
  local code
  code=$(curl -sS -o "$WORK/takeover_body" -w '%{http_code}' \
    -H 'Content-Type: application/octet-stream' \
    --data-binary @"$3" \
    "$BASE/v1/takeover?code=$1&local_seq=$2&independent=true")
  TAKEOVER_CODE=$code
  if [[ "$code" == 201 ]]; then
    json_field code < "$WORK/takeover_body" > "$WORK/new_code"
    NEW_CODE=$(cat "$WORK/new_code")
    echo "  POST /v1/takeover?code=$1&local_seq=$2&independent=true -> 201"
    echo "    新录制代号=${NEW_CODE:0:12}... 回看片号=$(json_field seq < "$WORK/takeover_body")"
  else
    echo "  POST /v1/takeover?code=$1&local_seq=$2 -> $code $(cat "$WORK/takeover_body")"
  fi
}

new_cursor() { # <seq> -> 设置全局 CURSOR 与 START_SEQ
  local code
  code=$(curl -sS -o "$WORK/start_body" -w '%{http_code}' "$BASE/v1/playback/start?seq=$1")
  echo "  GET /v1/playback/start?seq=$1 -> $code $(cat "$WORK/start_body")"
  [[ "$code" == 200 ]] || { echo "  起播失败"; exit 1; }
  START_SEQ=$(json_field start_seq < "$WORK/start_body")
  CURSOR=$(json_field cursor < "$WORK/start_body")
}

read_next() { # <outfile>；用全局 CURSOR，成功推进
  local code
  code=$(curl -sS -D "$WORK/headers" -o "$1" -w '%{http_code}' \
    "$BASE/v1/playback/read?cursor=$CURSOR")
  READ_CODE=$code; XSEQ=""
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
    parts.append("-" if s["empty"] else "slot%d:seq%d%s(g%d)" % (
        s["slot"], s["seq"], "I" if s["independent"] else " ", s["generation"]))
print("  当前录制代号: %s" % (
    "%s (第 %s 段录制%s)" % (
        st["code"], st["recording"]["ordinal"],
        "，本机片号偏移 %+d" % st["recording"]["offset"]
        if st["recording"]["offset"] else "")
    if st["recording"] else "（无，尚未接收初始录制）"))
print("  slots: " + ", ".join(parts))
print("  oldest=%s newest=%s earliest_playable=%s" % (
    st["oldest_seq"], st["newest_seq"], st["earliest_playable"]))'
}

say "0. 初始状态"
show_state

say "1. 主编码器首次上传（不带代号，仅适用于初始录制）：片 1 可独立解码"
make_seg 1 "$WORK/main_1.bin"
make_seg 2 "$WORK/main_2.bin"
upload 1 true - "$WORK/main_1.bin"
OLD_CODE=$(cat "$WORK/code")
echo "  初始录制代号=${OLD_CODE:0:12}..."
upload 2 false - "$WORK/main_2.bin"

say "2. 接替前：从片 1 起播，先读半片，保存"半片处"游标用于稍后跨接替点续读"
new_cursor 1
read_next "$WORK/mid1"
echo "  read -> $READ_CODE 片$XSEQ $(stat -c%s "$WORK/mid1") 字节（片 1 只读了一半）"
MID_CURSOR="$CURSOR"

say "3. 接替前先读到片 2 末尾（直播边缘），留一个接替点前的游标"
while true; do
  read_next "$WORK/pre"
  echo "  read -> $READ_CODE 片${XSEQ:-?} $(stat -c%s "$WORK/pre") 字节"
  [[ "$READ_CODE" == 200 ]] || break
done
END_CURSOR="$CURSOR"

say "4. 主编码器故障：值班员用当前代号提交备用编码器首片（本机片号 0，可独立解码）"
make_seg 100 "$WORK/backup_0.bin"
takeover "$OLD_CODE" 0 "$WORK/backup_0.bin"
NEW_CODE=$(cat "$WORK/new_code")
show_state

say "5. 接替生效后：旧代号上传 / 旧代号再次接替 / 无代号上传全部拒绝"
upload 3 false "$OLD_CODE" "$WORK/main_2.bin"
takeover "$OLD_CODE" 0 "$WORK/backup_0.bin"
upload 3 false - "$WORK/main_2.bin"
echo "  期望：404（旧代号）/ 404（旧代号接替）/ 409（接替后无代号）"

say "6. 备用编码器凭新代号，按自己的连续片号继续上传：本机 1 -> 回看片号 4"
make_seg 101 "$WORK/backup_1.bin"
upload 1 false "$NEW_CODE" "$WORK/backup_1.bin"
show_state

say "7. 接替前拿到的片 2 末尾游标，按原规则直接跨入接替首片"
CURSOR="$END_CURSOR"
read_next "$WORK/cross"
echo "  read -> $READ_CODE 片$XSEQ $(stat -c%s "$WORK/cross") 字节（跨过接替点）"

say "8. 更早的片 1 半片游标也能跨过接替点：续读到直播边缘"
CURSOR="$MID_CURSOR"; i=0
cat "$WORK/mid1" > "$WORK/resumed"
while true; do
  i=$((i+1))
  read_next "$WORK/part"
  echo "  read#$i -> $READ_CODE 片${XSEQ:-?} $(stat -c%s "$WORK/part") 字节"
  [[ "$READ_CODE" == 200 ]] || break
  cat "$WORK/part" >> "$WORK/resumed"
  [[ $i -lt 20 ]] || { echo "  异常：读的次数过多"; exit 1; }
done

say "9. 字节级校验"
cat "$WORK/main_1.bin" "$WORK/main_2.bin" "$WORK/backup_0.bin" "$WORK/backup_1.bin" > "$WORK/expected_all"
cmp "$WORK/expected_all" "$WORK/resumed" \
  && echo "  OK: 接替前游标续读 == 片1 + 片2 + 备用首片 + 备用本机片1"

# cross 从片 2 末尾开始，读出的是备用首片的前 64 KiB（单次上限）。
head -c "$(stat -c%s "$WORK/cross")" "$WORK/backup_0.bin" > "$WORK/expected_cross_prefix"
cmp "$WORK/expected_cross_prefix" "$WORK/cross" \
  && echo "  OK: 片2末尾游标跨入接替首片，字节一致"

say "10. 状态接口可查当前代号"
curl -sS "$BASE/v1/state" | python3 -c '
import sys, json
st = json.load(sys.stdin)
print("  code=%s newest_seq=%s" % (st["code"], st["newest_seq"]))
assert st["recording"]["ordinal"] == 2, st
assert st["recording"]["first_local_seq"] == 0, st
assert st["recording"]["last_local_seq"] == 1, st
print("  OK: 当前为第 2 段录制，备用编码器本机片号从 0 开始并继续到 1")'

say "演示完成"
