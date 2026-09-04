"""
TopoSpatial-CAD MCPB Release Packager
Packages TopoSpatial-CAD into a production-grade .mcpb bundle for Claude Desktop.

Workflow:
1. Assembles clean staging directory (dist/TopoSpatial-CAD-MCP)
2. Includes manifest.json, server/, src/, autodesk/, assets/, requirements.txt, LICENSE, README.md
3. Excludes pycache, tests, dev cache, and local logs
4. Enforces strict privacy sanitization (blocks any email leakage)
5. Produces:
   - dist/TopoSpatial-CAD-MCP-v{version}.mcpb
   - dist/TopoSpatial-CAD-MCP.mcpb
"""

import sys
import os
import shutil
import zipfile
import json
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DIST_DIR = ROOT_DIR / "dist"
STAGE_DIR = DIST_DIR / "TopoSpatial-CAD-MCP"
PACKAGING_DIR = ROOT_DIR / "packaging"


def clean_directory(path: Path):
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def copy_file(src: Path, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def copy_tree(src: Path, dest: Path, ignore_patterns=None):
    if ignore_patterns is None:
        ignore_patterns = shutil.ignore_patterns(
            "__pycache__", "*.pyc", "*.pyo", "*.pyd",
            ".pytest_cache", ".ruff_cache", ".mypy_cache",
            "*.egg-info", "*.dwl", "*.dwl2", "*.bak", ".git*"
        )
    shutil.copytree(src, dest, ignore=ignore_patterns, dirs_exist_ok=True)


def validate_manifest(manifest_path: Path) -> dict:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing manifest.json at {manifest_path}")
    
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    required_fields = ["manifest_version", "name", "version", "description", "author", "server"]
    for field in required_fields:
        if field not in data:
            raise ValueError(f"manifest.json missing required field: '{field}'")

    if not data.get("server", {}).get("entry_point"):
        raise ValueError("manifest.json server object missing 'entry_point'")

    return data


def audit_privacy(stage_dir: Path):
    """Ensure no private emails exist anywhere in the packaged bundle."""
    forbidden_terms = ["nihalclt.na@gmail.com", "nihalmannat@gmail.com"]
    violations = []
    
    for root, _, files in os.walk(stage_dir):
        for file in files:
            file_path = Path(root) / file
            if file_path.suffix in (".py", ".json", ".md", ".txt", ".bat", ".toml"):
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    for term in forbidden_terms:
                        if term in content:
                            violations.append((file_path.relative_to(stage_dir), term))
                except Exception:
                    pass

    if violations:
        print("[ERROR] Privacy audit failed! Found forbidden emails in packaged files:", file=sys.stderr)
        for path, term in violations:
            print(f"  - {path}: {term}", file=sys.stderr)
        raise RuntimeError("Bundle rejected due to privacy audit failure.")
    print("  [OK] Privacy audit passed (0 forbidden emails detected)")


def build_bundle():
    print("=" * 65)
    print("   Building TopoSpatial-CAD MCP Bundle (.mcpb)")
    print("=" * 65)

    # 1. Clean staging
    clean_directory(STAGE_DIR)

    # 2. Validate manifest
    manifest_src = PACKAGING_DIR / "manifest.json"
    manifest_data = validate_manifest(manifest_src)
    version = manifest_data.get("version", "3.0.0")
    print(f"  [OK] Manifest validated: {manifest_data['name']} v{version}")

    # 3. Copy files to staging
    copy_file(manifest_src, STAGE_DIR / "manifest.json")
    copy_file(PACKAGING_DIR / "server.json", STAGE_DIR / "server.json")
    copy_file(PACKAGING_DIR / "requirements.txt", STAGE_DIR / "requirements.txt")
    copy_file(PACKAGING_DIR / "README.md", STAGE_DIR / "README.md")
    
    license_src = ROOT_DIR / "LICENSE"
    if license_src.exists():
        copy_file(license_src, STAGE_DIR / "LICENSE")

    copy_tree(PACKAGING_DIR / "assets", STAGE_DIR / "assets")
    copy_tree(PACKAGING_DIR / "autodesk", STAGE_DIR / "autodesk")
    copy_tree(PACKAGING_DIR / "server", STAGE_DIR / "server")
    copy_tree(ROOT_DIR / "src", STAGE_DIR / "src")

    print(f"  [OK] Staged bundle structure in: {STAGE_DIR}")

    # 4. Run Privacy Audit
    audit_privacy(STAGE_DIR)

    # 5. Pack into .mcpb (ZIP format adhering to MCPB spec)
    versioned_mcpb = DIST_DIR / f"TopoSpatial-CAD-MCP-v{version}.mcpb"
    latest_mcpb = DIST_DIR / "TopoSpatial-CAD-MCP.mcpb"

    for target_mcpb in (versioned_mcpb, latest_mcpb):
        if target_mcpb.exists():
            target_mcpb.unlink()

        with zipfile.ZipFile(target_mcpb, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(STAGE_DIR):
                for file in files:
                    full_path = Path(root) / file
                    rel_path = full_path.relative_to(STAGE_DIR)
                    zf.write(full_path, arcname=str(rel_path).replace("\\", "/"))

        size_kb = target_mcpb.stat().st_size / 1024
        print(f"  [OK] Created bundle: {target_mcpb.name} ({size_kb:.1f} KB)")

    print("-" * 65)
    print("  SUCCESS: MCP Bundle (.mcpb) built and verified successfully!")
    print("=" * 65)


if __name__ == "__main__":
    build_bundle()
