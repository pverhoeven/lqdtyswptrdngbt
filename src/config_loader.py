"""
config_loader.py — laadt config/config.yaml en geeft een dict terug.
Alle modules importeren hieruit; pad-logica zit op één plek.
"""

import os
from pathlib import Path
import yaml

# Projectroot = de map boven src/ of scripts/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | None = None) -> dict:
    """
    Laad config.yaml.

    Parameters
    ----------
    path : str, optional
        Expliciet pad naar config-bestand. Standaard: <projectroot>/config/config.yaml

    Returns
    -------
    dict
        Volledige config als geneste dict.
    """
    if path is None:
        path = _PROJECT_ROOT / "config" / "config.yaml"

    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Maak data-paden absoluut t.o.v. projectroot
    for key, rel_path in cfg["data"]["paths"].items():
        cfg["data"]["paths"][key] = str(_PROJECT_ROOT / rel_path)

    return cfg


def project_root() -> Path:
    return _PROJECT_ROOT
