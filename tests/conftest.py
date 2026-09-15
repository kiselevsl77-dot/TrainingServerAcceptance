"""Общие фикстуры pytest пульта испытаний."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from acceptance.http_log import Journal
from client.http import ApiHttpClient
from client.settings import TrainingServerSettings


@pytest.fixture
def settings() -> TrainingServerSettings:
    """Настройки подключения к тестовому (несуществующему) серверу."""
    return TrainingServerSettings(base_url="http://test.local", timeout=5.0)


@pytest.fixture
def journal() -> Journal:
    """Пустой журнал обмена с сервером."""
    return Journal(max_records=100)


@pytest.fixture
def client_factory() -> Callable[..., ApiHttpClient]:
    """Фабрика httpx-клиента с подменённым транспортом (без реальной сети)."""

    def _factory(handler: Callable) -> ApiHttpClient:
        transport = httpx.MockTransport(handler)
        return ApiHttpClient(base_url="http://test.local", timeout=5.0, transport=transport)

    return _factory
