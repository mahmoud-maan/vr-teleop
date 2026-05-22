from pathlib import Path

__version__ = "0.1.0"

# Project root (the directory containing pyproject.toml). Used to resolve
# bundled assets like the URDF that live outside the import package.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = PROJECT_ROOT / "assets"
