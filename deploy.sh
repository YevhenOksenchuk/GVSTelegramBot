#!/usr/bin/env bash
# Оновлення бота на сервері: git pull → збірка → перезапуск → прибирання старих образів → логи.
# Запуск: /root/GVSTelegramBot/deploy.sh
# Тіло загорнуте у функцію, щоб git pull міг безпечно оновити й сам цей файл під час виконання.
set -euo pipefail

main() {
  cd "$(dirname "$(readlink -f "$0")")"

  echo "▶ Оновлюю код з GitHub…"
  git pull --ff-only

  BOT_VERSION="$(git rev-parse --short HEAD) · $(git log -1 --date=format:%d.%m.%Y --format=%cd)"
  export BOT_VERSION
  echo "▶ Збираю та запускаю версію: $BOT_VERSION"
  docker compose up -d --build

  echo "▶ Прибираю старі образи…"
  docker image prune -f >/dev/null

  echo "▶ Останні логи:"
  docker compose logs --tail=20
  echo
  echo "✅ Готово. У боті /stats має показати версію: $BOT_VERSION"
}

main "$@"
exit
