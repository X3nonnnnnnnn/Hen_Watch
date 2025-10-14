from __future__ import annotations
import json
import os
from typing import Dict, Any

STATE_FILE = "state.json"

def read_state(path: str = STATE_FILE) -> Dict[str, Any]:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def write_state(state: Dict[str, Any], path: str = STATE_FILE) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
