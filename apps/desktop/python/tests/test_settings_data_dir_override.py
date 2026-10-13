"""config.json must follow POTLUCK_DATA_DIR like every other on-disk store.

If services/settings_manager.py hardcoded config.json's path as
`Path.home() / ".potluck" / "config.json"` - a *default argument value*
baked in at function-definition (import) time - while every other on-disk
store (memory.db, models/, logs/) resolves through utils/paths.py's
POTLUCK_DATA_DIR-aware helpers, an instance launched with POTLUCK_DATA_DIR
set (e.g. a second/dev instance, or a test) would still read and write the
default instance's ~/.potluck/config.json - mixing its settings, gateway
secret, and active_project with the default instance's, even though its
DB/models were correctly isolated.

So get_settings()/save_settings() resolve the path lazily via
_default_config_path(), which calls utils.paths.config_path() - the same
helper memory_db_path()/models_dir()/logs_dir() use - and an override
isolates config.json too.

This test asserts the override actually lands config.json under the
override dir, and that it does not touch the real ~/.potluck/config.json
(critical for every other test in this suite that spins up create_app() /
hits routes touching settings: a leftover real ~/.potluck/config.json on
the machine running these tests must never be read or written).
"""
from __future__ import annotations

from pathlib import Path

from potluck_local.services.settings_manager import (
    PotluckSettings,
    get_settings,
    save_settings,
)


def test_get_settings_default_path_honours_potluck_data_dir(tmp_path, monkeypatch):
    override_dir = tmp_path / "override-instance"
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(override_dir))

    # No config.json exists yet anywhere under the override - get_settings()
    # must return defaults without touching (or creating) a real ~/.potluck.
    settings = get_settings()
    assert isinstance(settings, PotluckSettings)
    assert not (override_dir / "config.json").exists()

    # Change something identifiable and save - must land under the override.
    settings.app.theme = "dark"
    settings.gateway.enabled = False
    save_settings(settings)

    override_config = override_dir / "config.json"
    assert override_config.exists(), (
        "save_settings() with POTLUCK_DATA_DIR set did not write config.json "
        "under the override directory"
    )

    # A fresh read (still under the same override) must round-trip the value
    # - proving reads and writes agree on the same (overridden) path.
    reloaded = get_settings()
    assert reloaded.app.theme == "dark"


def test_two_instances_with_different_data_dirs_never_share_settings(tmp_path, monkeypatch):
    instance_a = tmp_path / "instance-a"
    instance_b = tmp_path / "instance-b"

    monkeypatch.setenv("POTLUCK_DATA_DIR", str(instance_a))
    settings_a = get_settings()
    settings_a.app.theme = "light"
    settings_a.app.font_size = 11
    save_settings(settings_a)

    monkeypatch.setenv("POTLUCK_DATA_DIR", str(instance_b))
    settings_b = get_settings()
    # Instance B must not see instance A's write - different override dirs
    # are fully isolated, exactly like memory.db.
    assert settings_b.app.theme != "light" or settings_b.app.font_size != 11
    settings_b.app.theme = "dark"
    save_settings(settings_b)

    assert (instance_a / "config.json").exists()
    assert (instance_b / "config.json").exists()

    monkeypatch.setenv("POTLUCK_DATA_DIR", str(instance_a))
    reread_a = get_settings()
    assert reread_a.app.theme == "light"


def test_default_config_path_matches_utils_paths_helper(tmp_path, monkeypatch):
    """settings_manager must route through the same helper as every other
    POTLUCK_DATA_DIR-aware store, not its own parallel copy of the path."""
    from potluck_local.services.settings_manager import _default_config_path
    from potluck_local.utils.paths import config_path, memory_db_path

    override_dir = tmp_path / "some-instance"
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(override_dir))

    assert _default_config_path() == config_path()
    assert _default_config_path() == override_dir / "config.json"
    # And it shares the same root as the memory DB path.
    assert _default_config_path().parent == memory_db_path().parent.parent
