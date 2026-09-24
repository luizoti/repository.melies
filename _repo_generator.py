#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Melies repository generator — multi-release (matrix / omega / piers).

Reads addons.yaml (declarative config), ensures submodule sources are checked
out, validates every addon (XML, assets, Kodi ABI vs release tree), rebuilds the
zips and addons.xml/addons.xml.md5 per release tree, patches the
repository.melies add-on <dir> entries, prunes stale zips and rewrites
index.html.

Usage:
    python3 _repo_generator.py --all --force --clean --bump-repo
    python3 _repo_generator.py --tree omega --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "addons.yaml"
INDEX_FILE = ROOT / "index.html"

REPO_EXT_POINT = "xbmc.addon.repository"
META_EXT_POINT = "xbmc.addon.metadata"
GUI_ADDON = "xbmc.gui"
PY_ADDON = "xbmc.python"
REPO_ADDON_ID = "repository.melies"

IGNORE_DIRS = {
    ".git", ".github", ".idea", ".vscode", "__pycache__",
    "venv", ".venv", "node_modules", "thumbs.db",
}
IGNORE_FILES = {".DS_Store", "thumbs.db", ".gitignore"}
IGNORE_SUFFIXES = {".pyc", ".pyo"}
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def _unquote(v: str):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _yaml_mini(text: str):
    """Tiny YAML subset: sections -> maps of scalars / nested maps / lists."""
    data: dict = {}
    section = None
    sub = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("- "):
            data.setdefault(section, {}).setdefault(sub, []).append(
                _unquote(line.strip()[2:])
            )
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, _, val = line.strip().partition(":")
        val = _unquote(val)
        if indent == 0:
            section = key
            sub = None
            data[section] = {} if not val else val
        elif indent <= 2:
            sub = key if not val else None
            data.setdefault(section, {})[key] = {} if not val else val
        else:
            data.setdefault(section, {}).setdefault(sub, {})[key] = val
    return data


def load_config():
    try:
        import yaml  # type: ignore

        cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except ImportError:
        cfg = _yaml_mini(CONFIG_FILE.read_text(encoding="utf-8"))
    meta = cfg.get("meta") or {}
    sources = cfg.get("sources") or {}
    releases = cfg.get("releases") or {}
    trees = cfg.get("trees") or {}
    for name, what in (("meta", meta), ("sources", sources), ("releases", releases), ("trees", trees)):
        if not isinstance(what, dict):
            sys.exit(f"addons.yaml malformed: '{name}' must be a mapping")
    return meta, sources, releases, trees


def quote(v):
    return v if re.fullmatch(r"[\w./:+_ -]+", str(v)) else json.dumps(str(v))


def save_config(meta, sources, releases, trees):
    out = [
        "# Melies repository — declarative config. Managed by _repo_generator.py.",
        "",
        "meta:",
    ]
    for k, v in meta.items():
        out.append(f"  {k}: {quote(v)}")
    out.append("")
    out.append("sources:")
    for k, v in sources.items():
        out.append(f"  {k}: {quote(v)}")
    out.append("")
    out.append("releases:")
    for name, rs in releases.items():
        out.append(f"  {name}:")
        for k, v in rs.items():
            out.append(f"    {k}: {quote(v)}")
    out.append("")
    out.append("trees:")
    for name, addons in trees.items():
        out.append(f"  {name}: [{', '.join(addons)}]")
    out.append("")
    CONFIG_FILE.write_text("\n".join(out), encoding="utf-8")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def version_ok(v) -> bool:
    # allow Kodi-style versions with alphanumeric suffix (e.g. "10.0.1-osd")
    return bool(re.fullmatch(r"\d+(\.\d+)*([-+][A-Za-z0-9._-]+)?", str(v)))


def version_tuple(v):
    return tuple(int(p) for p in str(v).split("."))


def bump_version(v):
    parts = [int(p) for p in str(v).split(".")]
    parts[-1] += 1
    return ".".join(str(p) for p in parts)


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def _ignored(p: Path, root: Path) -> bool:
    rel = p.relative_to(root)
    parts = rel.parts
    if any(part in IGNORE_DIRS for part in parts[:-1]):
        return True
    if p.name in IGNORE_DIRS or p.name in IGNORE_FILES:
        return True
    return p.suffix in IGNORE_SUFFIXES


def newest_mtime(addon_dir: Path) -> float:
    ts = 0.0
    for p in addon_dir.rglob("*"):
        if p.is_file() and not _ignored(p, addon_dir):
            ts = max(ts, p.stat().st_mtime)
    return ts


def build_zip(addon_dir: Path, zip_path: Path):
    files = sorted(
        p for p in addon_dir.rglob("*")
        if p.is_file() and not _ignored(p, addon_dir)
    )
    # Kodi packages must contain a single top-level folder named after the
    # addon id (archive "zip://path/addon.id/" + addon.xml). Flat layouts are
    # rejected by CAddonInstallJob ("invalid package").
    prefix = addon_dir.name
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zi = zipfile.ZipInfo(f"{prefix}/{f.relative_to(addon_dir).as_posix()}", ZIP_EPOCH)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.create_system = 3
            with f.open("rb") as fh:
                zf.writestr(zi, fh.read())


def parse_addon(addon_xml: Path):
    try:
        root = ET.parse(addon_xml).getroot()
    except ET.ParseError as exc:
        sys.exit(f"malformed XML in {addon_xml}: {exc}")
    version = root.get("version", "")
    if not version_ok(version):
        sys.exit(f"{addon_xml}: invalid or missing version '{version}'")
    requires = {}
    for imp in root.findall("requires/import"):
        aid = imp.get("addon")
        if aid:
            requires[aid] = imp.get("version", "")
    return root, root.get("id"), version, requires


def check_assets(addon_dir: Path, root) -> list:
    missing = []
    for ext in root.findall("extension"):
        if ext.get("point") in (META_EXT_POINT, "kodi.addon.metadata"):
            for asset in ext.findall("assets/*"):
                if asset.text and not (addon_dir / asset.text).exists():
                    missing.append(asset.text)
            break
    return missing


def abi_issues(release: dict, requires: dict, addon_id: str) -> list:
    issues = []
    gui = requires.get(GUI_ADDON)
    py = requires.get(PY_ADDON)
    if gui:
        gv = version_tuple(gui)
        if "gui_max" in release and gv > version_tuple(release["gui_max"]):
            issues.append(f"xbmc.gui {gui} > {release['gui_max']} supported by tree")
        if "gui_min" in release and gv < version_tuple(release["gui_min"]):
            issues.append(f"xbmc.gui {gui} < {release['gui_min']} required by tree ABI")
    if py and "python_min" in release and version_tuple(py) < version_tuple(release["python_min"]):
        issues.append(f"xbmc.python {py} < {release['python_min']} (Python 3 required)")
    return [f"{addon_id}: {i}" for i in issues]


# --------------------------------------------------------------------------
# per-addon operations
# --------------------------------------------------------------------------

def ensure_checkout(tree: str, addon_id: str, url: str, no_sync: bool) -> Path:
    path = ROOT / tree / addon_id
    if (path / ".git").exists() and (path / "addon.xml").exists():
        return path
    if no_sync:
        sys.exit(f"{tree}/{addon_id}: checkout missing and --no-sync set")
    if not url:
        sys.exit(f"{tree}/{addon_id}: no source URL in addons.yaml")
    if path.exists():
        shutil.rmtree(path)
    print(f"[sync] cloning {addon_id} -> {path.relative_to(ROOT)}")
    subprocess.run(["git", "clone", "--quiet", url, str(path)], check=True)
    return path


def warn_unexpected_folders(tree: str, manifest: set):
    for d in sorted((ROOT / tree).iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if d.name not in manifest and d.name not in ("zips", REPO_ADDON_ID):
            print(f"[warn] {tree}/{d.name}: not in trees manifest (ignored)")


def ensure_repo_addon_dirs(tree: str, repo_version: str, dry_run: bool):
    """Patch repository.melies/addon.xml <dir> entries and bump version."""
    entries = build_dir_entries(meta, releases)
    patch_repo_addon(ROOT / tree / REPO_ADDON_ID / "addon.xml", repo_version, entries, dry_run)


def build_dir_entries(meta: dict, releases: dict) -> list:
    base = str(meta["repo_base"]).rstrip("/")
    entries = []
    ordered = sorted(
        releases.items(),
        key=lambda kv: version_tuple(kv[1].get("minversion", "0.0.0")),
        reverse=True,
    )
    explicit = []
    for name, rs in ordered:
        if rs.get("default"):
            continue
        attrs = f' minversion="{rs["minversion"]}"'
        if rs.get("maxversion"):
            attrs += f' maxversion="{rs["maxversion"]}"'
        entries.append(_dir_block(name, base, attrs))
        explicit.append(rs)
    for name, rs in releases.items():
        if rs.get("default"):
            # cap the fallback dir below the lowest explicit minversion so that
            # exactly one <dir> matches any given Kodi version (avoids the index
            # being fetched twice and addons being listed twice)
            attrs = ""
            if explicit:
                lowest = min(version_tuple(e["minversion"]) for e in explicit)
                attrs = f' maxversion="{lowest[0] - 1}.99.99"'
            entries.append(_dir_block(name, base, attrs))
    return entries


def _dir_block(name: str, base: str, attrs: str) -> str:
    info = f"{base}/{name}/zips/addons.xml"
    return (
        f'        <dir{attrs}>\n'
        f'            <info compressed="false">{info}</info>\n'
        f'            <checksum>{base}/{name}/zips/addons.xml.md5</checksum>\n'
        f'            <datadir zip="true">{base}/{name}/zips/</datadir>\n'
        f"        </dir>"
    )


def patch_repo_addon(path: Path, version: str, entries: list, dry_run: bool):
    text = path.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r'(<addon\b[^>]*?\bversion=")[^"]*(")', rf"\g<1>{version}\g<2>", text, count=1
    )
    if not n:
        sys.exit(f"{path}: cannot find addon version attribute")
    match = re.search(
        r'(<extension\s+point="xbmc\.addon\.repository"[^>]*>).*?(</extension>)',
        new_text,
        re.S,
    )
    if not match:
        sys.exit(f"{path}: xbmc.addon.repository extension not found")
    block = match.group(1) + "\n" + "\n".join(entries) + "\n    " + match.group(2)
    new_text = new_text[: match.start()] + block + new_text[match.end():]
    if dry_run:
        print(f"[dry-run] would patch {path.relative_to(ROOT)} (version {version})")
        return
    path.write_text(new_text, encoding="utf-8")
    print(f"[repo] patched {path.relative_to(ROOT)} (version {version})")


def copy_meta(src: Path, zdir: Path, dry_run: bool):
    addon_xml = src / "addon.xml"
    target = zdir / "addon.xml"
    if not dry_run and (target.read_bytes() if target.exists() else b"") != addon_xml.read_bytes():
        shutil.copy(addon_xml, target)
    root = ET.parse(addon_xml).getroot()
    for ext in root.findall("extension"):
        if ext.get("point") in (META_EXT_POINT, "kodi.addon.metadata"):
            for asset in ext.findall("assets/*"):
                srcf = src / asset.text
                if asset.text and srcf.exists():
                    dst = zdir / asset.text
                    if not dry_run and (not dst.exists() or srcf.stat().st_size != dst.stat().st_size):
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy(srcf, dst)
            break


def write_addons_xml(tree: str, versions: dict, dry_run: bool):
    root_el = ET.Element("addons")
    for addon in sorted(versions):
        root_el.append(ET.parse(ROOT / tree / addon / "addon.xml").getroot())
    tree_el = ET.ElementTree(root_el)
    ET.indent(tree_el, space="  ")
    data = ET.tostring(root_el, encoding="utf-8", xml_declaration=True)
    zips_dir = ROOT / tree / "zips"
    zips_dir.mkdir(parents=True, exist_ok=True)
    out = zips_dir / "addons.xml"
    if dry_run:
        print(f"[dry-run] would write {out.relative_to(ROOT)} (+md5)")
        return
    out.write_bytes(data)
    (zips_dir / "addons.xml.md5").write_text(hashlib.md5(data).hexdigest(), encoding="utf-8")
    print(f"[xml ] {out.relative_to(ROOT)} + addons.xml.md5")


def clean_zips(tree: str, tree_addons: set, versions: dict, keep: int, dry_run: bool):
    zips_dir = ROOT / tree / "zips"
    for d in list(zips_dir.iterdir()):
        if d.is_dir() and d.name not in tree_addons:
            print(f"[clean] remove {d.relative_to(ROOT)} (addon not in tree)")
            if not dry_run:
                shutil.rmtree(d)
    for addon in sorted(tree_addons):
        d = zips_dir / addon
        if not d.is_dir():
            d.mkdir(parents=True)
        zips = sorted(d.glob(f"{addon}-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        current = f"{addon}-{versions.get(addon)}.zip"
        kept = 0
        for z in zips:
            if z.name == current:
                kept += 1
            elif kept < keep - 1:
                kept += 1
            else:
                print(f"[clean] remove {z.relative_to(ROOT)}")
                if not dry_run:
                    z.unlink()


def build_repo_zip(tree: str, repo_version: str, force: bool, dry_run: bool):
    repo_dir = ROOT / tree / REPO_ADDON_ID
    zdir = ROOT / tree / "zips" / REPO_ADDON_ID
    zdir.mkdir(parents=True, exist_ok=True)
    zip_path = zdir / f"{REPO_ADDON_ID}-{repo_version}.zip"
    if zip_path.exists() and not force:
        print(f"[skip] {zip_path.relative_to(ROOT)}")
    elif dry_run:
        print(f"[dry-run] would build {zip_path.relative_to(ROOT)}")
    else:
        print(f"[zip ] {zip_path.relative_to(ROOT)}")
        build_zip(repo_dir, zip_path)
        copy_meta(repo_dir, zdir, dry_run)


# --------------------------------------------------------------------------
# index.html
# --------------------------------------------------------------------------

def write_index(meta, releases, trees, versions_by_tree, dry_run):
    base = str(meta["repo_base"]).rstrip("/")
    repo_version = str(meta["repository_version"])
    h = []
    h.append("<!DOCTYPE html>")
    h.append('<html lang="en">')
    h.append("<head>")
    h.append('  <meta charset="utf-8">')
    h.append("  <title>Melies Repository</title>")
    h.append(
        '  <style>body{font-family:system-ui,sans-serif;max-width:64em;margin:2em auto;'
        'padding:0 1em;color:#222}table{border-collapse:collapse;width:100%}'
        'td,th{border:1px solid #ddd;padding:.4em .7em;text-align:left}'
        'th{background:#f4f4f4}a{color:#0645ad}</style>'
    )
    h.append("</head>")
    h.append("<body>")
    h.append("  <h1>Melies Repository</h1>")
    h.append(f'  <p>Add-on repository for Kodi · <a href="{base}">{meta.get("repository", "repository.melies")}</a></p>')
    h.append("  <h2>Installation</h2>")
    h.append(
        f'  <p>Download and install the repository add-on zip '
        f'(install from zip in Kodi): '
        f'<a href="{base}/omega/zips/{REPO_ADDON_ID}/{REPO_ADDON_ID}-{repo_version}.zip">'
        f'{REPO_ADDON_ID}-{repo_version}.zip</a></p>'
    )
    h.append("  <p>The repository self-selects the right release tree for your Kodi version.</p>")
    for name, rs in releases.items():
        h.append(f"  <h2>{name} · {rs.get('display', name)}</h2>")
        h.append("  <table><tr><th>Add-on</th><th>Version</th><th>Download</th></tr>")
        for addon in sorted(trees.get(name, [])):
            v = versions_by_tree.get(name, {}).get(addon, "?")
            href = f"{base}/{name}/zips/{addon}/{addon}-{v}.zip"
            h.append(f'    <tr><td>{addon}</td><td>{v}</td><td><a href="{href}">{addon}-{v}.zip</a></td></tr>')
        h.append("  </table>")
    h.append("</body>")
    h.append("</html>")
    if dry_run:
        print(f"[dry-run] would write {INDEX_FILE.relative_to(ROOT)}")
        return
    INDEX_FILE.write_text("\n".join(h) + "\n", encoding="utf-8")
    print(f"[html] wrote {INDEX_FILE.relative_to(ROOT)}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Melies multi-release repository generator")
    parser.add_argument("--tree", nargs="*", help="release trees to process (default: all existing)")
    parser.add_argument("--all", action="store_true", help="process every existing release tree")
    parser.add_argument("--force", action="store_true", help="rebuild zips even when up to date")
    parser.add_argument("--clean", action="store_true", help="remove stale zips (keeps --keep per addon)")
    parser.add_argument("--keep", type=int, default=5, help="latest zips kept per addon (default 5)")
    parser.add_argument("--dry-run", action="store_true", help="validate and show actions, write nothing")
    parser.add_argument("--no-sync", action="store_true", help="do not clone missing sources")
    parser.add_argument("--bump-repo", action="store_true", help="bump repository.melies patch version")
    args = parser.parse_args()

    global meta, sources, releases, trees
    meta, sources, releases, trees = load_config()

    existing = [r for r in releases if (ROOT / r).is_dir()]
    if args.all:
        selected = existing
    elif args.tree:
        selected = []
        for t in args.tree:
            if t not in releases:
                parser.error(f"unknown release tree '{t}' (known: {', '.join(releases)})")
            if not (ROOT / t).is_dir():
                parser.error(f"release tree directory '{t}' does not exist")
            selected.append(t)
    else:
        selected = existing
    if not selected:
        print("No release trees found; nothing to do.")
        return

    repo_version = str(meta["repository_version"])
    if not version_ok(repo_version):
        sys.exit(f"invalid repository_version '{repo_version}' in addons.yaml")
    if args.bump_repo and not args.dry_run:
        repo_version = bump_version(repo_version)
        meta["repository_version"] = repo_version
    print(f"Trees: {', '.join(selected)} | repository.melies -> {repo_version}")

    # 1) sync sources + validate (abort on first error, nothing written yet)
    versions = {}  # tree -> {addon: version}
    for tree in selected:
        manifest = set(trees.get(tree, []))
        warn_unexpected_folders(tree, manifest)
        for addon in sorted(manifest):
            path = ensure_checkout(tree, addon, sources.get(addon), args.no_sync)
            root_el, addon_id, version, requires = parse_addon(path / "addon.xml")
            if addon_id != addon:
                sys.exit(f"{path}: addon.xml id '{addon_id}' != expected '{addon}'")
            issues = abi_issues(releases[tree], requires, addon)
            if issues:
                sys.exit(f"[abi] {tree}: " + "; ".join(issues))
            missing = check_assets(path, root_el)
            if missing:
                sys.exit(f"{path}: missing assets: {', '.join(missing)}")
            recorded = run(["git", "ls-files", "--stage", "--", f"{tree}/{addon}"], cwd=ROOT).stdout
            head = run(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()
            if recorded.startswith("160000") and recorded.split()[1] != head:
                print(f"[warn] {tree}/{addon}: submodule recorded differs from checkout HEAD")
            print(f"[ok  ] {tree}/{addon} v{version}")
            versions.setdefault(tree, {})[addon] = version

    # 2) repository.melies: patch <dir> entries, sync copies, zip
    for tree in selected:
        ensure_repo_addon_dirs(tree, repo_version, args.dry_run)
        versions.setdefault(tree, {})[REPO_ADDON_ID] = repo_version
    if not args.dry_run and len(selected) > 1:
        canonical = (ROOT / selected[0] / REPO_ADDON_ID / "addon.xml").read_bytes()
        for tree in selected[1:]:
            path = ROOT / tree / REPO_ADDON_ID / "addon.xml"
            if path.read_bytes() != canonical:
                print(f"[repo] synced {path.relative_to(ROOT)}")
                path.write_bytes(canonical)

    # 3) zips + metadata per addon per tree
    for tree in selected:
        for addon in sorted(trees.get(tree, [])):
            src = ROOT / tree / addon
            zdir = ROOT / tree / "zips" / addon
            zdir.mkdir(parents=True, exist_ok=True)
            version = versions[tree][addon]
            zip_path = zdir / f"{addon}-{version}.zip"
            if zip_path.exists() and not args.force and newest_mtime(src) <= zip_path.stat().st_mtime:
                print(f"[skip] {zip_path.relative_to(ROOT)}")
            elif args.dry_run:
                print(f"[dry-run] would build {zip_path.relative_to(ROOT)}")
            else:
                print(f"[zip ] {zip_path.relative_to(ROOT)}")
                build_zip(src, zip_path)
            copy_meta(src, zdir, args.dry_run)
        build_repo_zip(tree, repo_version, args.force, args.dry_run)

    # 4) addons.xml + md5
    for tree in selected:
        write_addons_xml(tree, versions[tree], args.dry_run)

    # 5) clean stale zips
    if args.clean:
        for tree in selected:
            tree_addons = set(trees.get(tree, [])) | {REPO_ADDON_ID}
            clean_zips(tree, tree_addons, versions[tree], max(1, args.keep), args.dry_run)

    # 6) index.html + persist config
    write_index(meta, releases, trees, versions, args.dry_run)
    if args.bump_repo and not args.dry_run:
        save_config(meta, sources, releases, trees)
        print(f"[cfg ] {CONFIG_FILE.relative_to(ROOT)} updated to {repo_version}")

    if args.dry_run:
        print("[dry-run] no files were written — run without --dry-run to apply.")
    print("Done.")


if __name__ == "__main__":
    main()