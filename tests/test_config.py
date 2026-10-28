import pytest

from hamurai.config import ConfigError, load_config

VARIABLES = (
    "HAMURAI_COOKIE",
    "HAMURAI_ADMINISTRATION_ID",
    "HAMURAI_JOURNAL_ID",
    "HAMURAI_BASE_URL",
    "HAMURAI_APP_HEADER",
)
# Filled in unless a test passes its own value: most tests are about the other three.
CONNECTION = {"base_url": "https://hamurai.test", "app_header": "x-test-app: TEST"}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)


def write_env(tmp_path, **values):
    values = CONNECTION | values
    path = tmp_path / ".env"
    path.write_text("".join(f"HAMURAI_{key.upper()}={value}\n" for key, value in values.items()))
    return path


def test_reads_env_file(tmp_path):
    env = write_env(tmp_path, cookie="session=abc", administration_id="11111", journal_id="22222")

    config = load_config(env)

    assert config.cookie.get_secret_value() == "session=abc"
    assert config.administration_id == 11111
    assert config.journal_id == 22222


def test_environment_overrides_env_file(tmp_path, monkeypatch):
    env = write_env(tmp_path, cookie="session=abc", administration_id="11111", journal_id="22222")
    monkeypatch.setenv("HAMURAI_JOURNAL_ID", "33333")

    assert load_config(env).journal_id == 33333


def test_missing_variable_is_named(tmp_path):
    env = write_env(tmp_path, cookie="session=abc", administration_id="11111")

    with pytest.raises(ConfigError, match="HAMURAI_JOURNAL_ID"):
        load_config(env)


def test_empty_value_counts_as_missing(tmp_path):
    env = write_env(tmp_path, cookie="", administration_id="11111", journal_id="22222")

    with pytest.raises(ConfigError, match="HAMURAI_COOKIE"):
        load_config(env)


def test_error_never_contains_the_cookie(tmp_path):
    env = write_env(
        tmp_path,
        cookie="session=SECRET-VALUE",
        administration_id="11111",
        journal_id="not-a-number",
    )

    with pytest.raises(ConfigError) as err:
        load_config(env)

    assert "SECRET-VALUE" not in str(err.value)
    assert "HAMURAI_JOURNAL_ID" in str(err.value)


def test_repr_hides_the_cookie(tmp_path):
    env = write_env(tmp_path, cookie="session=abc", administration_id="11111", journal_id="22222")

    assert "abc" not in repr(load_config(env))


@pytest.mark.parametrize("cookie", ["Authorization=", "Authorization=   "])
def test_cookie_name_without_value_counts_as_missing(tmp_path, cookie):
    env = write_env(tmp_path, cookie=cookie, administration_id="11111", journal_id="22222")

    with pytest.raises(ConfigError, match="HAMURAI_COOKIE is missing"):
        load_config(env)


def test_cookie_without_name_is_invalid(tmp_path):
    env = write_env(tmp_path, cookie="SECRET-VALUE", administration_id="11111", journal_id="22222")

    with pytest.raises(ConfigError, match="HAMURAI_COOKIE is invalid") as err:
        load_config(env)

    assert "SECRET-VALUE" not in str(err.value)


def test_reads_base_url_and_app_header(tmp_path):
    env = write_env(tmp_path, cookie="session=abc", administration_id="11111", journal_id="22222")

    config = load_config(env)

    assert str(config.base_url) == "https://hamurai.test/"
    assert config.app_header_pair() == ("x-test-app", "TEST")


def test_app_header_is_split_on_the_first_colon(tmp_path):
    env = write_env(
        tmp_path,
        cookie="session=abc",
        administration_id="11111",
        journal_id="22222",
        app_header="x-a :b:c ",
    )

    assert load_config(env).app_header_pair() == ("x-a", "b:c")


@pytest.mark.parametrize("app_header", ["x-a", ": value", "x-a:  "])
def test_app_header_without_name_and_value_is_invalid(tmp_path, app_header):
    env = write_env(
        tmp_path,
        cookie="session=abc",
        administration_id="11111",
        journal_id="22222",
        app_header=app_header,
    )

    with pytest.raises(ConfigError, match="HAMURAI_APP_HEADER is invalid"):
        load_config(env)


def test_missing_base_url_is_named(tmp_path):
    env = write_env(
        tmp_path, cookie="session=abc", administration_id="11111", journal_id="22222", base_url=""
    )

    with pytest.raises(ConfigError, match="HAMURAI_BASE_URL is missing"):
        load_config(env)


def test_error_never_contains_the_app_header(tmp_path):
    env = write_env(
        tmp_path,
        cookie="session=abc",
        administration_id="11111",
        journal_id="not-a-number",
        app_header="SECRET-HEADER",
    )

    with pytest.raises(ConfigError) as err:
        load_config(env)

    assert "SECRET-HEADER" not in str(err.value)


def test_repr_hides_the_app_header(tmp_path):
    env = write_env(
        tmp_path,
        cookie="session=abc",
        administration_id="11111",
        journal_id="22222",
        app_header="x-a: SECRET-HEADER",
    )

    assert "SECRET-HEADER" not in repr(load_config(env))


@pytest.mark.parametrize(
    "base_url",
    ["https://hamurai.test/app", "https://hamurai.test/api/v2", "http://hamurai.test"],
)
def test_base_url_other_than_an_https_host_is_invalid(tmp_path, base_url):
    env = write_env(
        tmp_path,
        cookie="session=abc",
        administration_id="11111",
        journal_id="22222",
        base_url=base_url,
    )

    with pytest.raises(ConfigError, match="HAMURAI_BASE_URL is invalid"):
        load_config(env)
