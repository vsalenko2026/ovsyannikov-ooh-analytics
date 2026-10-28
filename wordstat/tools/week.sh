#!/usr/bin/env bash
# Недельный цикл выгрузки одной рутиной, по одному квотному окну за запуск.
#
# Сервис считает 100 вызовов в календарный час UTC, а полный цикл — 648
# вызовов. Раскладывать его на семь рутин по часам оказалось нежизнеспособно:
# доступ к репозиторию есть не у всех, и шесть из семи молча не доезжали.
# Поэтому цикл собран в один скрипт, который запускается ежедневно и делает
# за раз столько, сколько пускает квота, — неделя закрывается за несколько
# дней, а каждая сессия живёт минуты.
#
# Все запуски одной недели пишут в каталог, названный по её среде, поэтому
# кэш работает между днями: уже выгруженное не запрашивается и не
# оплачивается заново. Порядок работ — по убыванию важности: сначала
# недельный ряд, на нём считаются множители отчёта.
#
# Использование: bash tools/week.sh [дата среды]

set -euo pipefail
trap 'echo "ОШИБКА на строке $LINENO: $BASH_COMMAND" >&2' ERR
cd "$(dirname "$0")/.."

log()  { printf '\n>>> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }

# --- неделя -----------------------------------------------------------------
WEEK="${1:-$(python3 - <<'PY'
import datetime as dt
today = dt.datetime.now(dt.timezone.utc).date()
print((today - dt.timedelta(days=(today.weekday() - 2) % 7)).isoformat())
PY
)}"
log "неделя от среды $WEEK"

# --- окружение --------------------------------------------------------------
log "зависимости и доступы"
python3 -m pip install --quiet --disable-pip-version-check -r requirements.txt
python3 - <<'PYCHECK'
import importlib.util, sys
missing = [m for m in ("yaml", "requests", "openpyxl") if not importlib.util.find_spec(m)]
if missing:
    sys.exit("не установлены модули: " + ", ".join(missing))
sys.path.insert(0, ".")
from ws import config
key, folder, via_proxy = config.credentials()
print("    доступы на месте, ключ " + ("подставляет прокси" if via_proxy else "в переменных"))
PYCHECK

EXPECTED=$(python3 run.py plan | sed -n 's/^вызовов за прогон *: *//p' | tr -d ' ')
CORE_EXPECTED=$(python3 - <<'PY'
import sys; sys.path.insert(0, ".")
from ws import config
cfg = config.load()
print(3 * len(cfg.resolved_regions()) * len(cfg.devices))
PY
)
note "полная выгрузка — $EXPECTED вызовов, углублённый топ по ядру — $CORE_EXPECTED"

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

# --- конвейер ---------------------------------------------------------------
STOP=""          # причина остановки, если окно кончилось или что-то сломалось
DONE=()          # что реально сделано за этот запуск

ready() {        # каталог собран полностью?
  local dir="$1" need="$2"
  [ -d "$dir" ] || return 1
  local have; have=$(find "$dir" -maxdepth 1 -name '*.json' ! -name manifest.json | wc -l)
  [ "$have" -ge "$need" ]
}

job() {          # описание, затем команда
  [ -n "$STOP" ] && return 0
  local desc="$1"; shift
  log "$desc"
  local code=0
  set +e; "$@"; code=$?; set -e
  case "$code" in
    0) DONE+=("$desc") ;;
    5) STOP="прогон неполный, остановился на работе «$desc» — квота окна или череда отказов"
       note "$STOP" ;;
    *) STOP="работа «$desc» завершилась с кодом $code" ; note "$STOP" >&2 ;;
  esac
}

build_if_ready() {   # сборка бесплатна, но собирать половину выгрузки нельзя
  local dir="$1" need="$2" desc="$3"; shift 3
  [ -n "$STOP" ] && return 0
  if ready "$dir" "$need"; then
    log "$desc"
    "$@" && DONE+=("$desc")
  else
    note "«$desc» пропущена: выгрузка ещё неполная"
  fi
}

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

# --- сохранение -------------------------------------------------------------
log "сохранение результата"
git config user.email >/dev/null 2>&1 || git config user.email "noreply@anthropic.com"
git config user.name  >/dev/null 2>&1 || git config user.name  "Claude"
git add raw output

if git diff --cached --quiet; then
  note "нечего коммитить: за эту неделю всё уже выгружено"
else
  git commit -q -m "Выгрузка недели $WEEK, прогон $(date -u +%F)"
  note "закоммичено: $(git show --stat --oneline HEAD | tail -1)"
  BRANCH=$(git rev-parse --abbrev-ref HEAD)
  if ! git rev-parse --abbrev-ref '@{upstream}' >/dev/null 2>&1; then
    note "ветка без upstream — привязываю к origin/$BRANCH"
    git branch --quiet --set-upstream-to "origin/$BRANCH" "$BRANCH" 2>/dev/null || true
  fi
  if ! git pull --rebase --autostash --quiet; then
    echo "НЕ УДАЛОСЬ ПОДТЯНУТЬ ВЕТКУ: коммит есть локально, но не уехал." >&2
    exit 3
  fi
  if ! git push --quiet origin "$BRANCH"; then
    echo "ПУШ НЕ ПРОШЁЛ: коммит есть локально, но не уехал на GitHub." >&2
    exit 4
  fi
  note "запушено в $(git rev-parse --abbrev-ref HEAD)"
fi

# --- итог -------------------------------------------------------------------
log "итог прогона"
if [ ${#DONE[@]} -eq 0 ]; then
  note "ничего не делалось"
else
  for item in "${DONE[@]}"; do note "сделано: $item"; done
fi
for kind in "" "-day" "-top" "-top-core"; do
  dir="output/$WEEK$kind"
  [ -d "$dir" ] && note "готово: $dir"
done
[ -n "$STOP" ] && note "остановка: $STOP"
exit 0
