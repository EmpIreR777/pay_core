#!/bin/bash

message="$1"

if [ -z "$message" ]; then
    echo "Ошибка: Необходимо указать сообщение для ревизии: make alembic-rev m='add users'"
    exit 1
fi

uv run alembic revision -m "$message" --autogenerate
