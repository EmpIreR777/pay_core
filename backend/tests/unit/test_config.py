import os
from unittest.mock import patch

from src.core.config import Settings


def test_settings_default_values() -> None:
    """Проверка значений по умолчанию без кастомных переменных окружения."""
    settings = Settings(_env_file=None)

    assert settings.APP_NAME == 'payment-gateway-core'
    assert settings.API_PREFIX == '/api/v1'
    assert settings.API_TITLE == 'Payment Gateway'
    assert settings.DATABASE_URL == 'postgresql+asyncpg://postgres:postgres@localhost:5432/pay_core'
    assert settings.REDIS_URL == 'redis://localhost:6379/0'
    assert settings.KAFKA_BOOTSTRAP == 'localhost:9092'
    assert settings.HTTP_PORT == 8000
    assert settings.GRPC_PORT == 50051
    assert settings.OTEL_EXPORTER_OTLP_ENDPOINT == 'http://localhost:4317'
    assert settings.OTEL_SERVICE_NAME == 'payment-gateway'
    assert settings.JWT_SECRET.get_secret_value() == 'insecure-default-jwt-secret-change-me'
    assert settings.PAYMENT_PROVIDER == 'fake'
    assert settings.YOOKASSA_SHOP_ID is None
    assert settings.YOOKASSA_SECRET_KEY is None


def test_settings_loads_from_env() -> None:
    """Проверка загрузки всех параметров из переменных окружения (DoD T-0.3)."""
    env_vars = {
        'DATABASE_URL': 'postgresql+asyncpg://custom_user:custom_pass@postgres-host:5433/custom_db',
        'REDIS_URL': 'redis://redis-host:6380/2',
        'KAFKA_BOOTSTRAP': 'kafka-broker:9093',
        'HTTP_PORT': '8080',
        'GRPC_PORT': '50052',
        'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://otel-collector:4317',
        'OTEL_SERVICE_NAME': 'payment-gateway-custom',
        'JWT_SECRET': 'super-secret-jwt-key-for-test',
        'PAYMENT_PROVIDER': 'yookassa',
        'YOOKASSA_SHOP_ID': 'shop_123456',
        'YOOKASSA_SECRET_KEY': 'live_secret_key_abcdef',
    }

    with patch.dict(os.environ, env_vars, clear=False):
        loaded_settings = Settings(_env_file=None)

        assert (
            loaded_settings.DATABASE_URL == 'postgresql+asyncpg://custom_user:custom_pass@postgres-host:5433/custom_db'
        )
        assert loaded_settings.REDIS_URL == 'redis://redis-host:6380/2'
        assert loaded_settings.KAFKA_BOOTSTRAP == 'kafka-broker:9093'
        assert loaded_settings.HTTP_PORT == 8080
        assert loaded_settings.GRPC_PORT == 50052
        assert loaded_settings.OTEL_EXPORTER_OTLP_ENDPOINT == 'http://otel-collector:4317'
        assert loaded_settings.OTEL_SERVICE_NAME == 'payment-gateway-custom'
        assert loaded_settings.JWT_SECRET.get_secret_value() == 'super-secret-jwt-key-for-test'
        assert loaded_settings.PAYMENT_PROVIDER == 'yookassa'
        assert loaded_settings.YOOKASSA_SHOP_ID == 'shop_123456'
        assert loaded_settings.YOOKASSA_SECRET_KEY is not None
        assert loaded_settings.YOOKASSA_SECRET_KEY.get_secret_value() == 'live_secret_key_abcdef'


def test_settings_derives_sync_dsn_from_async() -> None:
    """Синхронный DSN (legacy-шим для Alembic) выводится из DATABASE_URL."""
    settings = Settings(_env_file=None)

    assert settings.SQLALCHEMY_SYNC_DB_URL == 'postgresql+psycopg2://postgres:postgres@localhost:5432/pay_core'
