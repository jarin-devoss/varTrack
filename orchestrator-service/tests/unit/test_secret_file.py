"""{file: …} credentials: the whole file, or one key of it."""
from __future__ import annotations

import pytest

from app.utils.secret_file import read_secret_file


@pytest.fixture
def files(tmp_path):
    def write(name, content):
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return str(p)
    return write


def test_whole_file_trimmed(files):
    assert read_secret_file(files("token", "  whole-file\n")) == "whole-file"


@pytest.mark.parametrize("key,want", [
    ("USER", "app"), ("PASSWORD", "p@ss word"), ("TOKEN", "abc"), ("EMPTY", ""), ("ESCAPED", 'say "hi"\n'),
])
def test_env_file(files, key, want):
    path = files("db.env", '# database\nexport USER=app\nPASSWORD="p@ss word"\nTOKEN=\'abc\'\nEMPTY=\n'
                           'ESCAPED="say \\"hi\\"\\n"\n')
    assert read_secret_file(path, key) == want


def test_json_and_yaml(files):
    j = files("creds.json", '{"db": {"password": "j-pw", "port": 5432, "tls": true}, "api.key": "flat"}')
    y = files("creds.yaml", "db:\n  password: y-pw\n  replicas: [a, b]\n")
    assert read_secret_file(j, "db.password") == "j-pw"
    assert read_secret_file(j, "db.port") == "5432"
    assert read_secret_file(j, "db.tls") == "true"
    assert read_secret_file(j, "api.key") == "flat"
    assert read_secret_file(y, "db.password") == "y-pw"
    with pytest.raises(ValueError, match="not a single value"):
        read_secret_file(y, "db.replicas")
    with pytest.raises(ValueError, match="has no key 'nope'"):
        read_secret_file(j, "nope")


def test_config_and_datasource_settings_read_a_key(files):
    from app.config import _resolve_secret_ref
    from app.pipeline.datasource_settings import find_datasource, sink_settings

    path = files("redis.env", "REDIS_PASSWORD=from-env-file\n")
    assert _resolve_secret_ref({"file": path, "key": "REDIS_PASSWORD"}) == "from-env-file"
    bundle = {"datasources": [{"redis": {"password": {"file": path, "key": "REDIS_PASSWORD"}}}]}
    kind, cfg = find_datasource(bundle, "redis")
    assert sink_settings(kind, cfg, "redis", bundle)["redis_password"] == "from-env-file"
