"""
A file's extension says its format before its name does (app/parsers/detector.py).

A name such as settings, variables or application stands for a format only when the file has no extension that says
another: settings.json is JSON, not the YAML "settings" stands for.
"""
import pytest

from app.parsers import loader
from app.parsers.detector import detect


@pytest.mark.parametrize("name,fmt", [
    # the cases the module's docstring gives
    ("package.json", "json"),
    ("appsettings.Production.json", "json"),
    (".env.staging", "kv"),
    ("config.toml", "toml"),
    ("web.config", "xml"),
    ("uwsgi.cfg", "ini"),
    ("variables.tfvars", "kv"),
    ("some_unknown_file", "sniff"),
    # a name whose prefix stands for another format than its extension
    ("variables.tf", "hcl"),
    ("main.tfvars", "kv"),
    ("settings.json", "json"),
    ("application.properties", "kv"),
    ("database.xml", "xml"),
    ("database.toml", "toml"),
    ("secrets.env", "kv"),
    ("values.yaml", "yaml"),
    ("outputs.tf", "hcl"),
    ("providers.tf", "hcl"),
    # a name with no known extension keeps the format its prefix stands for
    ("application", "yaml"),
    ("values", "yaml"),
    (".env.local", "kv"),
    (".envrc.local", "kv"),
    ("Dockerfile.prod", "kv"),
    ("settings.local", "yaml"),
    # the exact names
    (".env", "kv"),
    ("Makefile", "kv"),
    ("web.config", "xml"),
    ("app.config", "xml"),
    # a .config file that is not a known name is XML or INI by what it holds
    ("machine.config", "sniff"),
    ("settings.config", "sniff"),
])
def test_the_format_a_file_name_says(name, fmt):
    assert detect(name) == fmt


def test_a_directory_in_the_path_does_not_matter():
    assert detect("conf/prod/settings.json") == "json"
    assert detect("infra/variables.tf") == "hcl"


def test_a_properties_file_named_application_is_read_as_key_value_lines():
    text = "server.port=8080\nspring.application.name=app\n"
    assert loader.load_as(text, "application.properties") == {
        "server.port": "8080", "spring.application.name": "app"}


def test_a_toml_file_named_database_is_read_as_toml():
    assert loader.load_as('[db]\nhost = "h"\nport = 5432\n', "database.toml") == {"db": {"host": "h", "port": 5432}}
