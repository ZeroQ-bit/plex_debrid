import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


settings_bridge = load_module(
    "settings_bridge_test", ROOT / "web_ui" / "settings_bridge.py")
settings_file = load_module(
    "settings_file_test", ROOT / "plex_debrid" / "settings_file.py")


class SettingsPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = settings_bridge.SettingsStore(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def grouped_values(self):
        return {
            field["key"]: field["value"]
            for group in self.store.load_grouped()["groups"]
            for field in group["fields"]
        }

    def test_engine_content_names_render_and_symlinker_defaults_on(self):
        self.store.save_raw({
            "Content Services": ["Plex", "Trakt", "Overseerr"],
        })

        values = self.grouped_values()

        self.assertEqual(values["Content Services"], ["Plex", "Trakt", "Overseerr"])
        self.assertTrue(values["Symlinker Enabled"])
        self.assertEqual(values["Symlinker Interval"], "15")

    def test_historical_watchlist_labels_save_as_engine_names(self):
        self.store.apply_edits({
            "Content Services": ["Plex Watchlist", "Trakt Watchlist", "Overseerr"],
            "Symlinker Enabled": True,
        })

        raw = self.store.load_raw()
        self.assertEqual(raw["Content Services"], ["Plex", "Trakt", "Overseerr"])
        self.assertEqual(raw["Symlinker Enabled"], "true")

    def test_engine_save_preserves_web_ui_symlinker_settings(self):
        path = Path(self.temp_dir.name) / "settings.json"
        path.write_text(json.dumps({
            "Content Services": ["Plex"],
            "Symlinker Enabled": "true",
            "Symlinker Interval": "15",
        }))

        merged = settings_file.merge_settings_file(path, {
            "Content Services": ["Plex"],
            "Debug printing": "false",
        })

        self.assertEqual(merged["Symlinker Enabled"], "true")
        self.assertEqual(merged["Symlinker Interval"], "15")
        self.assertEqual(json.loads(path.read_text()), merged)

    def test_real_engine_reload_keeps_selected_content_and_web_ui_fields(self):
        script = r'''
import json
import os
import sys
import tempfile

sys.path.insert(0, os.environ["PD_TEST_ENGINE_ROOT"])
import ui

with tempfile.TemporaryDirectory() as config_dir:
    ui.config_dir = config_dir
    ui.save(doprint=False)
    path = os.path.join(config_dir, "settings.json")
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    data["Content Services"] = ["Plex Watchlist"]
    data["Symlinker Enabled"] = "true"
    data["Symlinker Interval"] = "15"
    data["Symlinker Mount Path"] = "/downloads"
    data["Symlinker Movies Library"] = "/downloads/vortexo/Movies"
    data["Symlinker TV Library"] = "/downloads/vortexo/TV"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)

    ui.load()

    with open(path, "r", encoding="utf-8") as fh:
        reloaded = json.load(fh)
    assert ui.content.services.active == ["Plex"], ui.content.services.active
    assert reloaded["Content Services"] == ["Plex"]
    assert reloaded["Symlinker Enabled"] == "true"
    assert reloaded["Symlinker TV Library"] == "/downloads/vortexo/TV"
'''
        environment = os.environ.copy()
        environment["PD_TEST_ENGINE_ROOT"] = str(ROOT / "plex_debrid")
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
