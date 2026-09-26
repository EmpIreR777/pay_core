from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent  # <project>/backend/src
BACKEND_DIR = BASE_DIR.parent
ENV_FILE_PATH = BACKEND_DIR / '.env'


class Settings(BaseSettings):
    """Единая конфигурация сервисов платёжного шлюза.

    Принцип 12-factor / single source of truth: подключения задаются одной
    DSN-строкой на инфраструктурный сервис (DATABASE_URL, REDIS_URL,
    KAFKA_BOOTSTRAP). Отдельные компоненты (host/port/user/... ) намеренно
    не дублируются — их всегда можно вычислить из DSN.
    """

    model_config = SettingsConfigDict(
        env_file=(str(ENV_FILE_PATH), '.env'),
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore',
    )

    # --- API metadata ---
    API_PREFIX: str = '/api/v1'
    API_VERSION: str = '0.1.0'
    API_TITLE: str = 'Payment Gateway'
    API_DESCRIPTION: str = 'Высоконадёжный платёжный шлюз Pay Core'

    # --- Application ---
    APP_NAME: str = 'payment-gateway-core'
    APP_ENV: str = 'development'
    LOG_LEVEL: str = 'INFO'
    DEFAULT_PAGE_SIZE: int = 10

    # --- Infrastructure DSN (single source of truth) ---
    DATABASE_URL: str = 'postgresql+asyncpg://postgres:postgres@localhost:5432/pay_core'
    REDIS_URL: str = 'redis://localhost:6379/0'
    KAFKA_BOOTSTRAP: str = 'localhost:9092'

    # --- Service ports ---
    HTTP_PORT: Annotated[int, Field(ge=1, le=65535)] = 8000
    GRPC_PORT: Annotated[int, Field(ge=1, le=65535)] = 50051

    # --- OpenTelemetry ---
    OTEL_EXPORTER_OTLP_ENDPOINT: str = 'http://localhost:4317'
    OTEL_SERVICE_NAME: str = 'payment-gateway'

    # --- Security / Auth ---
    JWT_SECRET: SecretStr = SecretStr('insecure-default-jwt-secret-change-me')
    JWT_ALGORITHM: str = 'HS256'
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # --- Payment provider ('fake' | 'yookassa') ---
    PAYMENT_PROVIDER: str = 'fake'
    YOOKASSA_SHOP_ID: str | None = None
    YOOKASSA_SECRET_KEY: SecretStr | None = None
    YOOKASSA_BASE_URL: str = 'https://api.yookassa.ru/v3'

    @computed_field  # type: ignore[prop-decorator]
    @property
    def SQLALCHEMY_SYNC_DB_URL(self) -> str:  # noqa: N802
        """LEGACY: синхронный DSN для старого ``alembic/env.py`` и ``run_migrations.py``.

        TODO: удалить вместе с переводом Alembic на async-паттерн
        и заменой legacy-раннера миграций. Новый код использует только
        ``DATABASE_URL`` (async, asyncpg).
        """
        return self.DATABASE_URL.replace('postgresql+asyncpg://', 'postgresql+psycopg2://')


settings = Settings()
