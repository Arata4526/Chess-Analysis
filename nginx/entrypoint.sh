#!/bin/sh
# Injecte l'API_KEY dans les fichiers HTML au démarrage du container
API_KEY="${API_KEY:-}"
if [ -n "$API_KEY" ]; then
  sed -i "s/__API_KEY__/$API_KEY/g" /usr/share/nginx/html/games.html
fi
exec nginx -g 'daemon off;'
