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
  new=$(sha256sum "$CODE/requirements.txt" 2>/dev/null | cut -d' ' -f1)
  old=$(cat "$STAMP" 2>/dev/null)
  if [ -n "$new" ] && [ "$new" != "$old" ]; then
    echo "Обновляю библиотеки…"
    if pip install --no-cache-dir -q -r "$CODE/requirements.txt"; then
      echo "$new" > "$STAMP"
    else
      # pip may already have changed part of the environment before failing.
      # Invalidate the stamp so rollback reinstalls its own requirements.
      rm -f "$STAMP"
      echo "Не удалось обновить библиотеки, продолжаю с имеющимися."
    fi
  fi
}

cd "$CODE" || exit 1
STOP=0
PID=""
trap 'STOP=1; [ -n "$PID" ] && kill -TERM "$PID" 2>/dev/null' TERM INT

while true; do
  sync_requirements
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
    echo "Получен запрос на обновление."
    python "$APP/updater.py"
    continue
  fi
  RUNTIME=$(( $(date +%s) - START ))
  if [ "$RC" -ne 0 ] && [ "$RUNTIME" -lt 180 ] && grep -q '"probation"' "$DATA/update_state.json" 2>/dev/null; then
    echo "Новая версия не запустилась (код $RC). Возвращаю предыдущую."
    python "$APP/updater.py" --rollback
    continue
  fi
  exit "$RC"
done
