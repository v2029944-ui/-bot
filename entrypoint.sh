#!/bin/sh
# Запуск бота в контейнере.
# Без GITHUB_REPO бот работает из образа. С GITHUB_REPO код берётся из репозитория (папка /app/code),
# а кнопка «🔄 Обновить» в боте (только админ) перезапускает его уже на новой версии.
APP="${APP_DIR:-/app}"
CODE="${CODE_DIR:-$APP/code}"
DATA="${DATA_DIR:-$APP/data}"
STAMP="${REQ_STAMP:-/opt/requirements.sha}"
export CODE_DIR="$CODE" DATA_DIR="$DATA"

if [ -z "${GITHUB_REPO:-}" ]; then
  cd "$APP" && exec python main.py
fi

if [ ! -f "$CODE/main.py" ]; then
  echo "Скачиваю код из GitHub (ветка ${GITHUB_BRANCH:-main})…"
  git clone --branch "${GITHUB_BRANCH:-main}" "$GITHUB_REPO" "$CODE" 2>&1 | sed 's#//[^/@]*@#//***@#'
  if [ ! -f "$CODE/main.py" ]; then
    echo "Не удалось скачать репозиторий. Проверьте GITHUB_REPO (адрес, токен) в .env."
    sleep 30
    exit 1
  fi
fi

sync_requirements() {
  [ -f "$CODE/requirements.txt" ] || return 1
  new=$(for req in requirements.txt requirements.lock requirements-dev.txt requirements-dev.lock; do
    if [ -f "$CODE/$req" ]; then cat "$CODE/$req"; fi
  done | sha256sum | cut -d' ' -f1)
  old=$(cat "$STAMP" 2>/dev/null)
  if [ -n "$new" ] && [ "$new" != "$old" ]; then
    echo "Обновляю библиотеки…"
    req="$CODE/requirements.txt"
    hash_flag=""
    if [ -f "$CODE/requirements-dev.lock" ]; then
      req="$CODE/requirements-dev.lock"
      hash_flag="--require-hashes"
    elif [ -f "$CODE/requirements.lock" ]; then
      req="$CODE/requirements.lock"
      hash_flag="--require-hashes"
    fi
    if pip install --no-cache-dir -q ${hash_flag:+"$hash_flag"} -r "$req"; then
      echo "$new" > "$STAMP"
    else
      # pip may already have changed part of the environment before failing.
      # Invalidate the stamp so rollback reinstalls its own requirements.
      rm -f "$STAMP"
      echo "Не удалось обновить библиотеки; запуск нового кода отменён."
      return 1
    fi
  fi
  return 0
}

cd "$CODE" || exit 1
STOP=0
PID=""
trap 'STOP=1; [ -n "$PID" ] && kill -TERM "$PID" 2>/dev/null' TERM INT
ROLLBACK_ATTEMPTED=0

while true; do
  if ! sync_requirements; then
    if [ "$ROLLBACK_ATTEMPTED" -eq 0 ] && grep -q '"probation"' "$DATA/update_state.json" 2>/dev/null; then
      ROLLBACK_ATTEMPTED=1
      python "$APP/updater.py" --rollback
      continue
    fi
    exit 1
  fi
  START=$(date +%s)
  python main.py &
  PID=$!
  wait "$PID"
  RC=$?
  if [ "$STOP" -eq 1 ]; then
    wait "$PID" 2>/dev/null
    exit 0
  fi
  PID=""
  if [ "$RC" -eq 42 ]; then
    ROLLBACK_ATTEMPTED=0
    echo "Получен запрос на обновление."
    python "$APP/updater.py"
    continue
  fi
  RUNTIME=$(( $(date +%s) - START ))
  if [ "$RC" -ne 0 ] && [ "$RUNTIME" -lt 180 ] && [ "$ROLLBACK_ATTEMPTED" -eq 0 ] && grep -q '"probation"' "$DATA/update_state.json" 2>/dev/null; then
    ROLLBACK_ATTEMPTED=1
    echo "Новая версия не запустилась (код $RC). Возвращаю предыдущую."
    python "$APP/updater.py" --rollback
    continue
  fi
  exit "$RC"
done
