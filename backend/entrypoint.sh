#!/bin/sh
set -e

cd /backend

echo "⏳ Выполняется миграция базы данных..."
PYTHONPATH=/backend python -m src.run_migrations

echo "✅ Миграция успешно завершена!"

echo "🚀 Запуск приложения..."
exec "$@"
