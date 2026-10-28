#!/usr/bin/env bash
# Недельный цикл выгрузки Вордстата одним скриптом.
#
# Сервис считает 100 вызовов в календарный час UTC, а полная неделя — 648
# вызовов, то есть семь окон. Скрипт идёт по работам в порядке важности,
# на отказе по квоте коммитит выгруженное и, если разрешено ждать,
# досыпает до следующего часа и продолжает.
#
# Все заходы одной недели пишут в каталог, названный по её среде, поэтому
# кэш работает между прогонами: уже полученное не запрашивается заново.
#
# Использование:
#   bash tools/week.sh                      один заход, сколько пускает квота
#   bash tools/week.sh --windows 8          добирать неделю, дожидаясь окон
#   bash tools/week.sh --week 2026-10-28 --windows 8

set -euo pipefail
trap 'echo "ОШИБКА на строке $LINENO: $BASH_COMMAND" >&2' ERR
cd "$(dirname "$0")/.."

log()  { printf '\n>>> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }

WEEK=""
WINDOWS=1
while [ $# -gt 0 ]; do
  case "$1" in
    --week)    WEEK="${2:?}"; shift 2 ;;
    --windows) WINDOWS="${2:?}"; shift 2 ;;
    *)         WEEK="$1"; shift ;;
  esac
done
[ -n "$WEEK" ] || WEEK=$(python3 -c "
import datetime as dt
t = dt.datetime.now(dt.timezone.utc).date()
print((t - dt.timedelta(days=(t.weekday() - 2) % 7)).isoformat())")

log "неделя от среды $WEEK, окон за прогон: $WINDOWS"

log "зависимости и доступы"
python3 -m pip install --quiet --disable-pip-version-check -r requirements.txt
python3 -c "
import importlib.util, sys
missing = [m for m in ('yaml','requests','openpyxl') if not importlib.util.find_spec(m)]
if missing: sys.exit('не установлены модули: ' + ', '.join(missing))
sys.path.insert(0, '.')
from ws import config
key, folder, via_proxy = config.credentials()
print('    доступы на месте, ключ ' + ('подставляет прокси' if via_proxy else 'в переменных'))"

EXPECTED=$(python3 run.py plan | sed -n 's/^вызовов за прогон *: *//p' | tr -d ' ')
CORE_EXPECTED=$(python3 -c "
import sys; sys.path.insert(0, '.')
from ws import config
c = config.load(); print(3 * len(c.resolved_regions()) * len(c.devices))")
note "полная выгрузка — $EXPECTED вызовов, топ по ядру — $CORE_EXPECTED"

only_args() {
  local index="$1" args=()
  while IFS= read -r phrase; do
    [ -n "$phrase" ] && args+=(--only "$phrase")
  done < <(python3 run.py chunks --index "$index")
  printf '%s\n' "${args[@]}"
}
mapfile -t GROUP1 < <(only_args 1)
mapfile -t GROUP2 < <(only_args 2)
CORE=(--only "овсянников мыло" --only "овсянников косметика" --only "овсянников купить")

ready() {
  local dir="$1" need="$2" have
  [ -d "$dir" ] || return 1
  have=$(find "$dir" -maxdepth 1 -name '*.json' ! -name manifest.json | wc -l)
  [ "$have" -ge "$need" ]
}

STOP=""
DONE=()

job() {
  [ -n "$STOP" ] && return 0
  local desc="$1"; shift
  log "$desc"
  local code=0
  set +e; "$@"; code=$?; set -e
  case "$code" in
    0) DONE+=("$desc") ;;
    5) STOP="прогон неполный, остановился на работе «$desc» — квота окна или череда отказов"
       note "$STOP" ;;
    *) STOP="работа «$desc» завершилась с кодом $code"; note "$STOP" >&2 ;;
  esac
}

build_if_ready() {
  local dir="$1" need="$2" desc="$3"; shift 3
  [ -n "$STOP" ] && return 0
  if ready "$dir" "$need"; then
    log "$desc"; "$@" && DONE+=("$desc")
  else
    note "«$desc» пропущена: выгрузка ещё неполная"
  fi
}

one_pass() {
  STOP=""
  job "недельный ряд, группа 1" python3 run.py fetch --run-date "$WEEK" "${GROUP1[@]}"
  job "недельный ряд, группа 2" python3 run.py fetch --run-date "$WEEK" "${GROUP2[@]}"
  build_if_ready "raw/$WEEK" "$EXPECTED" "сборка недельного ряда" \
    python3 run.py build --raw "raw/$WEEK" --out "output/$WEEK" --force

  job "дневной ряд, группа 1" python3 run.py fetch --period PERIOD_DAILY --run-date "$WEEK" "${GROUP1[@]}"
  job "дневной ряд, группа 2" python3 run.py fetch --period PERIOD_DAILY --run-date "$WEEK" "${GROUP2[@]}"
  build_if_ready "raw/$WEEK-day" "$EXPECTED" "сборка дневного ряда" \
    python3 run.py build --period PERIOD_DAILY --raw "raw/$WEEK-day" --out "output/$WEEK-day" --force

  job "топ слов, группа 1" python3 run.py top-export --run-date "$WEEK" "${GROUP1[@]}" --force
  job "топ слов, группа 2" python3 run.py top-export --run-date "$WEEK" "${GROUP2[@]}" --force
  job "углублённый топ по ядру" python3 run.py top-export --run-date "$WEEK" "${CORE[@]}" \
    --limit 200 --tag core --force
}

save() {
  log "сохранение результата"
  git config user.email >/dev/null 2>&1 || git config user.email "noreply@anthropic.com"
  git config user.name  >/dev/null 2>&1 || git config user.name  "Claude"
  git add raw output
  if git diff --cached --quiet; then
    note "нечего коммитить"
    return 0
  fi
  git commit -q -m "Выгрузка недели $WEEK, прогон $(date -u +%F\ %H:%M)"
  note "закоммичено: $(git show --stat --oneline HEAD | tail -1)"
  local branch; branch=$(git rev-parse --abbrev-ref HEAD)
  git pull --rebase --autostash --quiet || { echo "НЕ УДАЛОСЬ ПОДТЯНУТЬ ВЕТКУ" >&2; return 3; }
  git push --quiet origin "$branch" || { echo "ПУШ НЕ ПРОШЁЛ" >&2; return 4; }
  note "запушено в $branch"
}

sleep_to_next_hour() {
  local secs
  secs=$(python3 -c "
import datetime as dt
now = dt.datetime.now(dt.timezone.utc)
nxt = (now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1))
print(int((nxt - now).total_seconds()) + 20)")
  note "жду нового окна квоты: $((secs / 60)) мин $((secs % 60)) с"
  sleep "$secs"
}

for ((pass = 1; pass <= WINDOWS; pass++)); do
  log "=== заход $pass из $WINDOWS ==="
  one_pass
  save || true
  if [ -z "$STOP" ]; then
    note "неделя закрыта полностью"
    break
  fi
  [ "$pass" -lt "$WINDOWS" ] && sleep_to_next_hour
done

log "итог"
if [ ${#DONE[@]} -eq 0 ]; then note "ничего не делалось"; else
  for item in "${DONE[@]}"; do note "сделано: $item"; done
fi
for kind in "" "-day" "-top" "-top-core"; do
  [ -d "output/$WEEK$kind" ] && note "готово: output/$WEEK$kind"
done
[ -n "$STOP" ] && note "осталось: $STOP"
exit 0
