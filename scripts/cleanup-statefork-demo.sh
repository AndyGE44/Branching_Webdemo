#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

main_port="${DEMO_MAIN_PORT:-8000}"
branch_start="${DEMO_BRANCH_PORT_START:-8300}"
branch_end="${DEMO_BRANCH_PORT_END:-8350}"
sessions_dir="${CHECKPOINT_SESSIONS_DIR:-/tmp/checkpoint-sessions-mailbox-demo}"
statefork_cwd="${DEMO_STATEFORK_CWD:-/users/alexxjk/StateFork}"

kill_listeners() {
  local port="$1"
  local pids
  pids="$(sudo lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    echo "Stopping listener on port ${port}: ${pids}"
    sudo kill $pids 2>/dev/null || true
  fi
}

kill_listeners "$main_port"
for port in $(seq "$branch_start" "$branch_end"); do
  kill_listeners "$port"
done

sleep 1

force_kill_listeners() {
  local port="$1"
  local pids
  pids="$(sudo lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    echo "Force stopping listener on port ${port}: ${pids}"
    sudo kill -9 $pids 2>/dev/null || true
  fi
}

force_kill_listeners "$main_port"
for port in $(seq "$branch_start" "$branch_end"); do
  force_kill_listeners "$port"
done

unmount_session_dir() {
  local dir="$1"
  local attempt

  for attempt in $(seq 1 20); do
    mapfile -t targets < <(
      {
        findmnt -R -n -r -o TARGET "$dir" 2>/dev/null || true
        mount | grep -F "$dir" | awk '{print $3}' || true
      } \
        | awk 'NF' \
        | awk '{print length($0), $0}' \
        | sort -rn \
        | cut -d' ' -f2- \
        | awk '!seen[$0]++'
    )

    [[ "${#targets[@]}" -gt 0 ]] || break

    for target in "${targets[@]}"; do
      sudo umount -l "$target" 2>/dev/null || true
    done

    sleep 0.2
  done
}

if [[ -d "$sessions_dir" ]]; then
  if [[ -x "${statefork_cwd}/checkpoint-lite" ]]; then
    for session_path in "$sessions_dir"/*; do
      [[ -d "$session_path" ]] || continue
      session_id="$(basename "$session_path")"
      echo "Cleaning checkpoint-lite session ${session_id}"
      (cd "$statefork_cwd" && sudo ./checkpoint-lite cleanup "$session_id" --force) 2>/dev/null || true
    done
  fi

  unmount_session_dir "$sessions_dir"
  sudo rm -rf "$sessions_dir"
fi

echo "StateFork demo cleanup complete."
