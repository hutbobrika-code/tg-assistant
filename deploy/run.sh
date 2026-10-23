#!/usr/bin/env bash
# Бот и Gemini-роутер на сервере без systemd (например, в песочнице Daytona).
# Каждый процесс крутится в своём цикле и поднимается сам, если упал; логи в logs/.
#   deploy/run.sh start   - запустить (повторный запуск ничего не дублирует)
#   deploy/run.sh stop    - остановить
#   deploy/run.sh status  - что сейчас работает
set -u
cd "$(dirname "$0")/.."
export LANG=C.UTF-8 PYTHONUTF8=1  # в голых контейнерах локаль ASCII, а история и логи на русском
ROUTER_URL=https://github.com/zexadev/gemini-web2api-go/releases/download/v4.22.0/gemini-web2api-go_v4.22.0_linux_amd64
SELF="$(pwd)/deploy/run.sh"

keep() {
  name=$1
  shift
  while true; do
    echo "== $(date '+%F %T') старт" >> "logs/$name.log"
    "$@" >> "logs/$name.log" 2>&1
    echo "== $(date '+%F %T') выход с кодом $?, перезапуск через 5 с" >> "logs/$name.log"
    sleep 5
  done
}

running() {
  pgrep -f "run.sh keep $1" > /dev/null
}

decrypt() {
  # state/*.enc шифрует Actions-версия бота; расшифровываем один раз, дальше бот пишет сам
  [ -f "$2" ] || [ ! -f "$1" ] || STATE_KEY="$STATE_KEY" openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 \
    -salt -pass env:STATE_KEY -in "$1" -out "$2" || echo "не расшифровался $1 - проверьте STATE_KEY"
}

case "${1:-start}" in
  keep)
    shift
    keep "$@"
    ;;
  start)
    mkdir -p logs data workspace
    if ! grep -q '^TG_TOKEN=.' .env 2> /dev/null; then
      echo "нет TG_TOKEN в .env"
      exit 1
    fi
    STATE_KEY=$(grep -m1 '^STATE_KEY=' .env | cut -d= -f2-)
    if [ -n "$STATE_KEY" ]; then
      decrypt state/state.enc state.json
      decrypt state/gemini.db.enc data/gemini.db
    fi
    if [ ! -x gemini-web2api-go ]; then
      curl -fsSL -o gemini-web2api-go "$ROUTER_URL" && chmod +x gemini-web2api-go
    fi
    running router || nohup setsid "$SELF" keep router ./gemini-web2api-go --port 8083 \
      --api-key sk-gemini-internal --db data/gemini.db > /dev/null 2>&1 &
    running bot || nohup setsid "$SELF" keep bot python3 -u bot.py > /dev/null 2>&1 &
    sleep 1
    "$SELF" status
    ;;
  stop)
    pkill -f "run.sh keep" || true
    pkill -f "gemini-web2api-go --port 8083" || true
    pkill -f "python3 -u bot.py" || true
    echo "остановлено"
    ;;
  status)
    for name in router bot; do
      if running $name; then echo "$name: работает"; else echo "$name: остановлен"; fi
    done
    ;;
  *)
    echo "использование: $0 start|stop|status"
    exit 1
    ;;
esac
