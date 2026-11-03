"""Build a plugin archive from an explicit allowlist, never credentials/caches."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
files = [root / name for name in (
    "__init__.py", "main.py", "metadata.yaml", "_conf_schema.json",
    "requirements.txt", "README.md",
)]
files += sorted((root / "codex_images").glob("*.py"))
files += sorted(file for file in (root / "views").rglob("*") if file.suffix in {".html", ".js", ".css"})
destination = root / "dist" / "astrbot_plugin_codex_images-v0.3.0.zip"
destination.parent.mkdir(exist_ok=True)
with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
    for file in files:
        archive.write(file, "astrbot_plugin_codex_images/" + file.relative_to(root).as_posix())
with ZipFile(destination) as archive:
    assert archive.testzip() is None
    assert len(archive.namelist()) == len(files)
print(destination)
