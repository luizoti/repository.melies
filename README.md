# Melies repository for Kodi — multi-release

Repository dedicated to storing new addons or modified versions of others already exist.

The repo is organized per Kodi release tree. Kodi self-selects the correct tree
from the repository add-on `<dir>` entries (highest `minversion` ≤ installed
version wins). Piers is a mirror of Omega: the skin's ABI (xbmc.gui 5.17.0)
runs on both Kodi 21 and Kodi 22.

## Trees

| Tree   | Kodi        | Add-ons |
|--------|-------------|---------|
| matrix | 19 / 20     | plugin.video.crunchyroll, script.library.integration.tool |
| omega  | 21 (Omega)  | skin.xperience1080, plugin.video.crunchyroll, script.library.integration.tool |
| piers  | 22 (Piers)  | skin.xperience1080, plugin.video.crunchyroll, script.library.integration.tool |

`matrix/skin.xperience1080` was moved to `omega/` and `piers/` — the skin requires
`xbmc.gui 5.17.0` (Omega ABI) and does not install on Matrix/Nexus.

## Installing

1. Download the repository add-on zip and install from zip in Kodi:
   `omega/zips/repository.melies/repository.melies-0.1.11.zip`
   (works for Kodi 19–22; or manually: `https://raw.githubusercontent.com/luizoti/repository.melies/master/omega/zips/repository.melies/repository.melies-0.1.11.zip`)
2. Open **Add-ons → Install from repository → Melies** and install the add-ons for your version.

## Sources (submodules)

Each add-on is a git submodule (own remote). Layout mirrors the tree:

| Add-on | URL |
|--------|-----|
| plugin.video.crunchyroll | https://github.com/luizoti/plugin.video.crunchyroll |
| script.library.integration.tool | https://github.com/luizoti/script.library.integration.tool |
| skin.xperience1080 | https://github.com/luizoti/skin.xperience1080 |

## Regenerating the repository

Everything is driven by `addons.yaml` (declarative) + `_repo_generator.py`:

```sh
python3 _repo_generator.py --all --dry-run     # validate without writing
python3 _repo_generator.py --all --force --clean --bump-repo
```

What the generator does per tree: syncs/clones submodule sources, validates
addon.xml, assets and Kodi ABI vs the release tree (fail-loud), rebuilds zips,
rewrites `zips/addons.xml` + `.md5`, patches the `repository.melies` add-on
`<dir>` entries (and bumps its version with `--bump-repo`), prunes stale zips
(`--clean`, keeps the latest `--keep` per add-on) and regenerates `index.html`.

GitHub Actions (`.github/workflows/release.yml`) runs the generator on every
push to `master` and commits the regenerated artifacts.