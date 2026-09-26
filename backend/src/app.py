import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.core.config import settings

logging.basicConfig(level=settings.LOG_LEVEL.upper(), format='%(levelname)s: %(name)s - %(message)s')

app = FastAPI(version=settings.API_VERSION, title=settings.API_TITLE, description=settings.API_DESCRIPTION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        'http://localhost:3000',
        'http://127.0.0.1:3000',
    ],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

# TODO(epic-07): подключить роутеры BFF (app.include_router(api_router)).
# Роутеры FastAPI-шлюза появляются в ЭПИК 7; здесь оставлен только каркас app.
