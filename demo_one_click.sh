#!/usr/bin/env bash
# ============================================================
#  一键演示脚本 · 给 HR 实机演示用 (Linux / macOS / Git Bash)
#
#  用途：把「体检 → 跑一次 → 演示紧急停止 → 恢复」串成一条命令，
#        避免现场手打参数打错、漏打、顺序乱。
#
#  用法：
#     bash demo_one_click.sh              # 完整演示（含 kill switch）
#     bash demo_one_click.sh --no-switch  # 只跑，不演示停止开关
#     bash demo_one_click.sh --check      # 只做体检（开场前 30 分钟跑这个）
#
#  ★ 变量名用英文：Git Bash 在部分 locale 下不认中文变量名。
# ============================================================

set -u

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXE="$PROJ/dist/dist-win/简报助手.exe"
OUTDIR="$PROJ/briefings"

CLIENT="${DEMO_CLIENT:-招商银行}"
INDUSTRY="${DEMO_INDUSTRY:-金融}"
TOPIC="${DEMO_TOPIC:-数据中台建设}"

DO_SWITCH=1
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --no-switch) DO_SWITCH=0 ;;
    --check)     CHECK_ONLY=1 ;;
  esac
done

hr() { printf '\n%s\n' "=============================================================="; }

find_python() {
  if [ -x "$PROJ/.venv-agents/Scripts/python.exe" ]; then
    echo "$PROJ/.venv-agents/Scripts/python.exe"
  elif [ -x "$PROJ/.venv/bin/python" ]; then
    echo "$PROJ/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  elif command -v python >/dev/null 2>&1; then
    command -v python
  else
    echo ""
  fi
}
PY="$(find_python)"

# ---------- 1) 体检 ----------
hr
echo "  (1) 部署体检"
hr
if [ -f "$EXE" ]; then
  "$EXE" --check || true
else
  echo "  ! 没找到 exe：$EXE"
  echo "    请先跑 build_package.py 打包"
fi

if [ "$CHECK_ONLY" = "1" ]; then
  exit 0
fi

# ---------- 2) 跑一次 ----------
hr
echo "  (2) 现场运行（HR 看着进度走完，这段等待是资产）"
hr
if [ -f "$EXE" ]; then
  "$EXE" --client "$CLIENT" --industry "$INDUSTRY" --topic "$TOPIC"
  RUNCODE=$?
else
  echo "  ! 跳过：exe 不存在"
  RUNCODE=1
fi

if [ "$RUNCODE" != "0" ]; then
  echo
  echo "  [!] 没跑通 —— 改走「念成品简报」路径："
  ls -1t "$OUTDIR"/*.md 2>/dev/null | head -3 | while read -r f; do
    echo "    $f"
  done
fi

# ---------- 3) 紧急停止开关 ----------
if [ "$DO_SWITCH" = "1" ]; then
  hr
  echo "  (3) 紧急停止开关演示（客户现场说停 → 立刻停）"
  hr

  if [ -n "$PY" ]; then
    echo "  -> 关掉："
    # 优先用 exe 内置开关（客户机上没有 Python 也能用）
    if [ -f "$EXE" ]; then
      "$EXE" --kill-switch off || true
    else
      "$PY" "$PROJ/kill_switch.py" off "演示：客户要求暂停" || true
    fi
    echo
    echo "  -> 再跑一次，应该被挡住："
    if [ -f "$EXE" ]; then
      "$EXE" --client "$CLIENT" --industry "$INDUSTRY" --topic "$TOPIC"
      echo "    退出码 = $?（3 = 被停用挡住，这是设计好的专用码）"
    fi
    echo
    echo "  -> 恢复："
    if [ -f "$EXE" ]; then
      "$EXE" --kill-switch on || true
    else
      "$PY" "$PROJ/kill_switch.py" on "已确认恢复" || true
    fi
  else
    echo "  ! 找不到 python，改用 exe 内置开关："
    if [ -f "$EXE" ]; then
      "$EXE" --kill-switch off || true
      echo "  -> 再跑一次，应该被挡住："
      "$EXE" --client "$CLIENT" --industry "$INDUSTRY" --topic "$TOPIC"
      echo "    退出码 = $?（3 = 被停用挡住）"
      "$EXE" --kill-switch on || true
    else
      echo "  ! exe 也不存在，跳过开关演示"
    fi
  fi
fi

hr
echo "  演示结束。成品简报在：$OUTDIR"
hr
