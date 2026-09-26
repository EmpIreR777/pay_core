from pathlib import Path

from dotenv import load_dotenv
from pydantic import computed_field
from pydantic_core import MultiHostUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent  # <project>/backend/src
ENV_FILE_PATH = BASE_DIR.parent / '.env'  # <project>/backend/.env
STORAGE_DIR = BASE_DIR / 'storage' / 'files'
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(ENV_FILE_PATH, override=True)


class Settings(BaseSettings):
    """Настройки приложения, загружаемые из переменных окружения и .env файла."""

    model_config = SettingsConfigDict(env_file=str(ENV_FILE_PATH))

    API_PREFIX: str = '/api/v1'
    API_VERSION: str = '0.1.0'
    API_TITLE: str = 'Pay Core'
    API_DESCRIPTION: str = 'Project Pay Core'

    POSTGRES_HOST: str
    POSTGRES_PORT: int
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str

    REDIS_HOST: str
    REDIS_PORT: int

    LOG_LEVEL: str = 'INFO'

    DEFAULT_PAGE_SIZE: int = 3

    def build_database_url(self, scheme: str) -> str:
        return str(
            MultiHostUrl.build(
                scheme=scheme,
                username=self.POSTGRES_USER,
                password=self.POSTGRES_PASSWORD,
                host=self.POSTGRES_HOST,
                port=self.POSTGRES_PORT,
                path=self.POSTGRES_DB,
            ),
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def SQLALCHEMY_DB_URL(self) -> str:  # noqa: N802
        return self.build_database_url('postgresql+asyncpg')

    @computed_field  # type: ignore[prop-decorator]
    @property
    def SQLALCHEMY_SYNC_DB_URL(self) -> str:  # noqa: N802
        return self.build_database_url('postgresql+psycopg2')

    @property
    def REDIS_URL(self) -> str:  # noqa: N802
        return f'redis://{self.REDIS_HOST}:{self.REDIS_PORT}'


settings = Settings()
