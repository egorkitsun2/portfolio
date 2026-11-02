"""Package the existing static portfolio using an explicit allowlist."""
from pathlib import Path
import hashlib
import json
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "release" / "portfolio-v0.3-rc1"
ARCHIVE = TARGET.parent / (TARGET.name + ".zip")
CODE = ["index.html", "contact.css", "contact.js", "review.css",
        "portfolio.js", "project-data.js", "project-media.js",
        "favicon.svg", "404.html", "robots.txt", "sitemap.xml", "assets/social/portfolio-card.png"]
data_text = (ROOT / "project-data.js").read_text(encoding="utf-8")
data = json.loads(data_text[data_text.index("{"):data_text.rindex(";")])
selected = [Path(name) for name in CODE]
selected += [path.relative_to(ROOT) for path in sorted((ROOT / "projects").glob("*.html"))]
for project in data.values():
    for shot in project["screenshots"]:
        selected.append(Path(shot["src"]))
    video = project.get("video")
    if video:
        selected.extend(Path(video[key]) for key in ("src", "poster", "captions") if video.get(key))
selected = sorted(set(selected))
for relative in selected:
    source = (ROOT / relative).resolve()
    if not source.is_relative_to(ROOT) or not source.is_file():
        raise SystemExit(f"Missing or unsafe input: {relative}")
    if any(part.startswith(".") for part in relative.parts):
        raise SystemExit(f"Hidden input is forbidden: {relative}")
TARGET.mkdir(parents=True, exist_ok=True)
expected = {p.as_posix() for p in selected} | {"manifest.json"}
unexpected = [p for p in TARGET.rglob("*") if p.is_file() and p.relative_to(TARGET).as_posix() not in expected]
if unexpected:
    raise SystemExit("Unexpected files in release directory; review them manually before building.")
manifest = {}
for relative in selected:
    destination = TARGET / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / relative, destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    assert digest == hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
    manifest[relative.as_posix()] = digest
(TARGET / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
temporary = ARCHIVE.with_suffix(".tmp.zip")
with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
    for relative in [*selected, Path("manifest.json")]:
        archive.write(TARGET / relative, relative.as_posix())
temporary.replace(ARCHIVE)
print(f"Ready: {len(selected)} files")
print(TARGET)
print(ARCHIVE)
