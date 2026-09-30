#!/bin/sh
set -e

cd /backend

echo "🚀 Запуск приложения..."
exec "$@"
