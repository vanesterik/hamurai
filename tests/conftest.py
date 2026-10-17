from collections.abc import AsyncIterator

import pytest
from fake_api import ADMINISTRATION_ID, JOURNAL_ID, FakeHamurai
from pydantic import SecretStr

from hamurai.client import HamuraiClient
from hamurai.config import Config

COOKIE = "session=SECRET-VALUE"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def config() -> Config:
    return Config.model_construct(
        cookie=SecretStr(COOKIE), administration_id=ADMINISTRATION_ID, journal_id=JOURNAL_ID
    )


@pytest.fixture
def fake() -> FakeHamurai:
    return FakeHamurai()


@pytest.fixture
async def client(config: Config, fake: FakeHamurai) -> AsyncIterator[HamuraiClient]:
    client = HamuraiClient(config, transport=fake.transport())
    yield client
    await client.aclose()
