"""Dynaconf-based configuration for Parsec."""

from dynaconf import Dynaconf

config = Dynaconf(
    envvar_prefix="PARSEC",
    settings_files=[
        "config/config.yaml",
        "config/config.local.yaml",
    ],
    environments=False,
    load_dotenv=False,
    merge_enabled=True,
)


def get_config() -> Dynaconf:
    return config
