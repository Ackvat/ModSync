# ModSync

A small CLI tool that syncs a Minecraft modpack's mods between a local
folder and a GitHub repository. It is built around a "base" snapshot
(the unmodified CurseForge modpack) so that only the mods *you* added
or removed travel through Git - the rest of the modpack is left alone.

## Files in this folder

| File | Purpose |
|---|---|
| `modsync.py` | The full source. Single file, stdlib only - works on any system with Python 3.8+. |
| `modsync` | A standalone Linux executable built with PyInstaller (no Python required). |
| `README.md` | This file. |

## Running it

**With Python (any OS):**
```
python3 modsync.py
```

**Linux executable (no Python needed):**
```
./modsync
```

**Building a Windows `.exe` yourself**: PyInstaller can only build for the
OS it runs on, so to get a `modsync.exe` you need to run this *on Windows*:

```
pip install pyinstaller
pyinstaller --onefile --name modsync --console modsync.py
```

The result will be in `dist\modsync.exe`.

## Settings storage

All settings are stored as JSON under:

- **Windows:** `%APPDATA%\ModSync\` (i.e. `AppData\Roaming\ModSync`)
- **macOS:** `~/Library/Application Support/ModSync/`
- **Linux:** `~/.config/ModSync/`

Files used: `config.json` (paths + GitHub creds), `base.json` (base mod
list), `history.json` (last synced version).

## Typical flow

### Person who maintains the modpack (uploader)

1. **Set Mod Folder** - point at your `.minecraft/mods` (or instance mods folder).
2. **Set GitHub** - paste `owner/repo` and a Personal Access Token with
   `repo` scope. Create one at <https://github.com/settings/tokens>.
3. **Set Base** - run this *right after* installing the unmodified
   CurseForge modpack. These mods will be ignored by sync.
4. Add or remove mods to your folder however you like.
5. **Sync mods → Upload** - the tool computes what changed against base
   and the repo state, uploads only the new `.jar`s, and writes a
   `manifest.json` for the new version (`v1`, `v2`, ...).

### Other players (downloaders)

1. **Set Mod Folder** - point at their mods folder.
2. **Set GitHub** - paste the same `owner/repo`. A token is only needed
   for a private repo.
3. **Set Base** - same base modpack, set right after installing it.
4. **Sync mods → Download** - the tool fetches the latest manifest,
   computes the difference, removes mods they shouldn't have, and
   downloads the missing ones from the right version folders.

A late joiner who has only the base can run Download once and arrive
at the latest state in a single step - the tool walks every version's
`new_jars` list to locate each mod file.

## Repo layout produced by the tool

```
mods/
  v1/
    manifest.json
    cool_mod.jar
    another_mod.jar
  v2/
    manifest.json
    yet_another_mod.jar
```

Each `manifest.json` records the cumulative target state
(`target_added`, `target_removed`) plus `new_jars` for the mod files
physically contained in that version's folder. Mods that should be
*removed* are recorded by name only - no `.jar` is uploaded for those.

## Limitations

- The GitHub Contents API caps individual file uploads at 100MB. Most
  Minecraft mods are well below that, but very large mods will be
  skipped with a warning.
- Authentication uses a Personal Access Token. Full OAuth flow is not
  implemented - PATs are simpler and equally effective for a CLI tool.
- The tool only tracks `.jar` files in the top level of the mods folder.
