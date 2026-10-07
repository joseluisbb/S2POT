import os
import json

TUNING_FILENAME = "s2pot_tuning.json"

DEFAULT_GLOBAL_TUNING = {
    "color_mode": "color",       # Options: "color", "grayscale", "monochrome"
    "target_dpi": 300,
    "jpeg_quality": 70,
    "downsample_max_dim": 2000
}


def get_tuning_file_path(input_dir):
    """Returns the expected path of the s2pot_tuning.json file in the given input folder."""
    if not input_dir or not os.path.exists(input_dir):
        return None
    return os.path.join(input_dir, TUNING_FILENAME)


def load_tuning_config(input_dir):
    """
    Loads s2pot_tuning.json from the input directory if it exists.
    Returns a dict with 'global_tuning' and 'file_tunings'.
    """
    tuning_file = get_tuning_file_path(input_dir)
    if not tuning_file or not os.path.isfile(tuning_file):
        return None

    try:
        with open(tuning_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "global_tuning" in data:
                return data
    except Exception as e:
        print(f"Error loading {TUNING_FILENAME} from {input_dir}: {e}")

    return None


def save_tuning_config(input_dir, global_tuning=None, file_tunings=None):
    """
    Saves the tuning configuration to s2pot_tuning.json in the specified input directory.
    """
    if not input_dir or not os.path.exists(input_dir):
        return False

    tuning_file = os.path.join(input_dir, TUNING_FILENAME)

    config_data = {
        "version": "1.0",
        "global_tuning": global_tuning or dict(DEFAULT_GLOBAL_TUNING),
        "file_tunings": file_tunings or {}
    }

    try:
        with open(tuning_file, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error saving {TUNING_FILENAME} to {input_dir}: {e}")
        return False


def get_effective_file_tuning(tuning_config, filename):
    """
    Given a loaded tuning_config dict and a filename (basename),
    returns the effective tuning parameters combining global tuning and per-file overrides.
    """
    effective = dict(DEFAULT_GLOBAL_TUNING)

    if not tuning_config or not isinstance(tuning_config, dict):
        return effective

    global_t = tuning_config.get("global_tuning", {})
    if isinstance(global_t, dict):
        effective.update(global_t)

    file_t = tuning_config.get("file_tunings", {}).get(filename)
    if isinstance(file_t, dict):
        effective.update(file_t)

    return effective
