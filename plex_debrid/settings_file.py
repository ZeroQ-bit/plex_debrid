"""Safe settings.json merge helpers shared by the legacy engine."""
import json
import os
import tempfile


def merge_settings_file(path, managed_settings):
    """Atomically update engine fields while preserving Web UI-owned fields."""
    path = os.fspath(path)
    existing = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            existing = loaded
    except (OSError, json.JSONDecodeError):
        pass

    existing.update(managed_settings)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=directory,
                prefix=".settings-", delete=False) as fh:
            temporary = fh.name
            json.dump(existing, fh, indent=4)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return existing
