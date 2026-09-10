#!/usr/bin/env bash
# Один шаг еженедельного расписания. Шаги разнесены по часам, потому что
# сервис считает 100 вызовов в календарный час UTC: каждый шаг умещается
# в своё окно и завершается за минуты, а не ждёт границы часа.
#
#   1  недельный ряд, группа фраз 1
#   2  недельный ряд, группа фраз 2, сборка
#   3  дневной ряд, группа фраз 1
#   4  дневной ряд, группа фраз 2, сборка
#   5  топ слов, группа фраз 1
#   6  топ слов, группа фраз 2, сборка
#   7  углублённый топ по ядру метрики, сборка
#
# Использование: bash tools/step.sh <номер шага>

set -euo pipefail
cd "$(dirname "$0")/.."

STEP="${1:?укажите номер шага, 1..7}"
RUN_DATE="$(date -u +%F)"

only_args() {                       # фразы группы -> набор --only
  local index="$1" args=()
  while IFS= read -r phrase; do
    [ -n "$phrase" ] && args+=(--only "$phrase")
  done < <(python3 run.py chunks --index "$index")
  printf '%s\n' "${args[@]}"
}

mapfile -t GROUP1 < <(only_args 1)
mapfile -t GROUP2 < <(only_args 2)

CHUNKS="$(python3 run.py chunks | tail -1)"
case "$CHUNKS" in
  *"всего групп: 2"*) ;;
  *) echo "Расписание рассчитано на две группы фраз, сейчас иначе: $CHUNKS" >&2
     echo "Пересоберите расписание под новое число групп." >&2; exit 2 ;;
esac

case "$STEP" in
  1) python3 run.py fetch "${GROUP1[@]}" ;;
  2) python3 run.py fetch "${GROUP2[@]}"
     python3 run.py build --force ;;
  3) python3 run.py fetch --period PERIOD_DAILY "${GROUP1[@]}" ;;
  4) python3 run.py fetch --period PERIOD_DAILY "${GROUP2[@]}"
     python3 run.py build --period PERIOD_DAILY --force ;;
  5) python3 run.py top-export "${GROUP1[@]}" --force ;;
  6) python3 run.py top-export "${GROUP2[@]}" --force ;;   # сборка увидит обе группы
  7) python3 run.py top-export --only "овсянников мыло" --only "овсянников косметика" \
                               --only "овсянников купить" --limit 200 --tag core --force ;;
  *) echo "Неизвестный шаг: $STEP (ожидается 1..7)" >&2; exit 2 ;;
esac

# Результат должен пережить сессию: контейнер эфемерный.
git config user.email >/dev/null 2>&1 || git config user.email "noreply@anthropic.com"
git config user.name  >/dev/null 2>&1 || git config user.name  "Claude"
git add raw output
if git diff --cached --quiet; then
  echo "нечего коммитить: всё уже было в кэше"
else
  git commit -q -m "Выгрузка $RUN_DATE, шаг $STEP"
  git pull --rebase --quiet
  git push --quiet
  echo "закоммичено и запушено"
fi
