from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fake_api import ADMINISTRATION_ID, JOURNAL_ID, FakeHamurai
from mcp import Client
from pydantic import HttpUrl, SecretStr

from hamurai.client import HamuraiClient
from hamurai.config import Config
from hamurai.server import build_server

COOKIE = "session=SECRET-VALUE"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def config() -> Config:
    return Config.model_construct(
        cookie=SecretStr(COOKIE),
        administration_id=ADMINISTRATION_ID,
        journal_id=JOURNAL_ID,
        base_url=HttpUrl("https://hamurai.test"),
        app_header=SecretStr("x-test-app: TEST"),
    )


@pytest.fixture
def fake() -> FakeHamurai:
    return FakeHamurai()


@pytest.fixture
async def client(config: Config, fake: FakeHamurai) -> AsyncIterator[HamuraiClient]:
    client = HamuraiClient(config, transport=fake.transport())
    yield client
    await client.aclose()


@asynccontextmanager
async def connected(client: HamuraiClient, path: Path) -> AsyncGenerator[Client]:
    """A session with the server, on the fake Hamurai and the suggestions at path."""
    async with Client(build_server(client, path)) as session:
        yield session


def text(result) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)
