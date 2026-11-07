from pathlib import Path

from app.config import KEY_FILES, Settings, read_key_file


def write_keys(directory: Path, **keys: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for provider, value in keys.items():
        (directory / KEY_FILES[provider]).write_text(value)
    return directory


def test_only_openai_and_anthropic_key_files_exist():
    assert KEY_FILES == {"openai": ".openai_api_key", "anthropic": ".anthropic_api_key"}


def test_keys_come_only_from_key_files(tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "JERSEY_OPENAI_API_KEY", "JERSEY_ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "from-environment")
    dotenv = tmp_path / "test.env"
    dotenv.write_text("OPENAI_API_KEY=from-dotenv\nANTHROPIC_API_KEY=from-dotenv\nJERSEY_OPENAI_API_KEY=from-dotenv\n")

    s = Settings(key_file_dir=tmp_path / "keys", _env_file=dotenv)
    assert (s.openai_api_key, s.anthropic_api_key) == ("", "")

    write_keys(tmp_path / "keys", openai="sk-file-123\n")
    s = Settings(key_file_dir=tmp_path / "keys", _env_file=dotenv)
    assert s.openai_api_key == "sk-file-123"
    assert s.anthropic_api_key == ""


def test_key_cannot_be_injected_by_init_kwargs(tmp_path):
    s = Settings(key_file_dir=tmp_path, _env_file=None, openai_api_key="injected", anthropic_api_key="injected")
    assert s.openai_api_key == "" and s.anthropic_api_key == ""


def test_read_key_file_tolerates_common_paste_mistakes(tmp_path):
    cases = {
        "plain": ("sk-proj-Example\n", "sk-proj-Example"),
        "spaces": ("   sk-abc   \n\n", "sk-abc"),
        "quoted": ('"sk-quoted"', "sk-quoted"),
        "assignment": ("OPENAI_API_KEY=sk-assigned", "sk-assigned"),
        "export": ("export ANTHROPIC_API_KEY='sk-ant-exported'", "sk-ant-exported"),
        "comment": ("# paste below\nsk-after-comment\n", "sk-after-comment"),
        "bom": ("﻿sk-bom", "sk-bom"),
        "empty": ("", ""),
    }
    for name, (content, expected) in cases.items():
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        assert read_key_file(path) == expected, name
    assert read_key_file(tmp_path / "missing") == ""


def test_key_status_never_reveals_keys(tmp_path):
    write_keys(tmp_path, anthropic="sk-ant-secret-value")
    s = Settings(key_file_dir=tmp_path, _env_file=None)
    status = s.key_status
    assert status["anthropic"] == "loaded from .anthropic_api_key"
    assert status["openai"] == ".openai_api_key is empty"
    assert "sk-ant-secret-value" not in repr(status)
    assert "sk-ant-secret-value" not in s.model_dump_json()


def test_settings_use_prefixed_env_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_EFFORT", "max")  # e.g. set by unrelated tools
    monkeypatch.setenv("JERSEY_OPENAI_IMAGE_MODEL", "gpt-image-test")
    s = Settings(key_file_dir=tmp_path, _env_file=None)
    assert s.claude_effort == "medium"
    assert s.openai_image_model == "gpt-image-test"
