#!/usr/bin/env python3
"""
ModSync - Minecraft Modpack Sync Tool

A CLI tool for syncing Minecraft modpack mods between a local mods folder
and a GitHub repository. Supports uploading and downloading the difference
between a "base" modpack (e.g. from CurseForge) and the current state.
"""

import os
import sys
import json
import base64
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

VERSION = "1.0.0"


# ===================== Configuration & Storage =====================

def get_config_dir() -> Path:
    """Resolve config directory cross-platform.

    Per the spec, on Windows this is %APPDATA%/ModSync (i.e.
    AppData/Roaming/ModSync). On other platforms a sensible equivalent
    is used.
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    p = Path(base) / "ModSync"
    p.mkdir(parents=True, exist_ok=True)
    return p


CONFIG_DIR = get_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
BASE_FILE = CONFIG_DIR / "base.json"
HISTORY_FILE = CONFIG_DIR / "history.json"


def _load_json(path: Path, default):
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    # Return a fresh copy so callers can mutate safely
    return json.loads(json.dumps(default))


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_config() -> dict:
    return _load_json(CONFIG_FILE, {
        "mod_folder": "",
        "github_repo": "",      # owner/repo
        "github_branch": "main",
        "github_token": "",
    })


def save_config(cfg: dict) -> None:
    _save_json(CONFIG_FILE, cfg)


def load_base() -> list:
    return _load_json(BASE_FILE, {"mods": []}).get("mods", [])


def save_base(mods: list) -> None:
    _save_json(BASE_FILE, {"mods": sorted(mods)})


def load_history() -> dict:
    return _load_json(HISTORY_FILE, {"last_synced_version": None})


def save_history(h: dict) -> None:
    _save_json(HISTORY_FILE, h)


# ===================== UI helpers =====================

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def banner(title: str):
    line = "=" * 60
    print(line)
    print(f"  {title}")
    print(line)


def ask(message: str) -> str:
    try:
        return input(f"{message}: ").strip()
    except EOFError:
        return ""


def confirm(message: str, default_yes: bool = False) -> bool:
    suffix = "(Y/n)" if default_yes else "(y/N)"
    answer = ask(f"{message} {suffix}").lower()
    if not answer:
        return default_yes
    return answer in ("y", "yes")


def pause():
    try:
        input("\nPress Enter to continue...")
    except EOFError:
        pass


# ===================== GitHub API =====================

class GitHubError(Exception):
    pass


class GitHub:
    """Tiny GitHub REST API client (stdlib only)."""

    API = "https://api.github.com"

    def __init__(self, repo: str, token: str = "", branch: str = "main"):
        self.repo = repo  # "owner/repo"
        self.token = token
        self.branch = branch or "main"

    def _headers(self, accept: str = "application/vnd.github+json") -> dict:
        h = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"ModSync/{VERSION}",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    @staticmethod
    def _encode_path(path: str) -> str:
        """URL-encode each path segment so filenames with spaces or special
        characters (parentheses, brackets, etc.) don't break the request."""
        if "?" in path:
            p, q = path.split("?", 1)
        else:
            p, q = path, ""
        encoded = "/".join(
            urllib.parse.quote(seg, safe="") for seg in p.split("/")
        )
        return f"{encoded}?{q}" if q else encoded

    def _request(self, method: str, path: str, data=None, accept=None,
                 raw_response: bool = False, timeout: int = 60):
        url = f"{self.API}{self._encode_path(path)}"
        headers = self._headers(accept) if accept else self._headers()
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read()
                if raw_response:
                    return content
                if not content:
                    return {}
                return json.loads(content)
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = ""
            raise GitHubError(f"HTTP {e.code} on {method} {path}: {err_body[:300]}")
        except urllib.error.URLError as e:
            raise GitHubError(f"Network error: {e.reason}")

    # ---- Repo / auth ----

    def check_repo_access(self) -> bool:
        try:
            self._request("GET", f"/repos/{self.repo}")
            return True
        except GitHubError:
            return False

    # ---- Contents API ----

    def list_dir(self, path: str) -> list:
        try:
            result = self._request(
                "GET", f"/repos/{self.repo}/contents/{path}?ref={self.branch}"
            )
            return result if isinstance(result, list) else []
        except GitHubError as e:
            if "HTTP 404" in str(e):
                return []
            raise

    def get_file_meta(self, path: str):
        """Return metadata dict for a file (incl. sha) or None if missing."""
        try:
            return self._request(
                "GET", f"/repos/{self.repo}/contents/{path}?ref={self.branch}"
            )
        except GitHubError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    def get_file(self, path: str):
        """Download a file's bytes via raw media type (works for files up to 100MB).
        Returns None if the file doesn't exist."""
        try:
            return self._request(
                "GET",
                f"/repos/{self.repo}/contents/{path}?ref={self.branch}",
                accept="application/vnd.github.raw",
                raw_response=True,
                timeout=300,
            )
        except GitHubError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    def put_file(self, path: str, content: bytes, message: str) -> None:
        """Create or update a file. Files >100MB are not supported here."""
        if len(content) > 100 * 1024 * 1024:
            raise GitHubError(
                f"File '{path}' is larger than 100MB; the GitHub Contents API "
                "cannot upload it. Skipping."
            )
        meta = self.get_file_meta(path)
        data = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
            "branch": self.branch,
        }
        if meta and isinstance(meta, dict) and meta.get("sha"):
            data["sha"] = meta["sha"]
        self._request("PUT", f"/repos/{self.repo}/contents/{path}", data=data,
                      timeout=300)


# ===================== Mod-sync core logic =====================

def list_jars(folder: Path) -> list:
    if not folder.exists():
        return []
    return sorted(
        f.name for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() == ".jar"
    )


def list_versions(gh: GitHub) -> list:
    """Return version folders (e.g. ['v1','v2','v3']) sorted ascending."""
    versions = []
    for item in gh.list_dir("mods"):
        if item.get("type") == "dir":
            name = item.get("name", "")
            if name.startswith("v") and name[1:].isdigit():
                versions.append(name)
    versions.sort(key=lambda v: int(v[1:]))
    return versions


def next_version(versions: list) -> str:
    if not versions:
        return "v1"
    return f"v{int(versions[-1][1:]) + 1}"


def load_manifest(gh: GitHub, version: str) -> dict:
    raw = gh.get_file(f"mods/{version}/manifest.json")
    if raw is None:
        return {
            "version": version,
            "target_added": [],
            "target_removed": [],
            "new_jars": [],
        }
    return json.loads(raw.decode("utf-8"))


def repo_state(gh: GitHub):
    """Return (target_added, target_removed, versions). The latest manifest
    holds the cumulative target state."""
    versions = list_versions(gh)
    if not versions:
        return [], [], []
    latest = load_manifest(gh, versions[-1])
    return (
        list(latest.get("target_added", [])),
        list(latest.get("target_removed", [])),
        versions,
    )


def build_jar_location_map(gh: GitHub, versions: list) -> dict:
    """Map each jar filename to the version folder that physically holds it."""
    locations = {}
    for v in versions:
        try:
            m = load_manifest(gh, v)
        except GitHubError:
            continue
        for jar in m.get("new_jars", []):
            # Last writer wins (a jar re-uploaded later lives in the newer version)
            locations[jar] = v
    return locations


# ===================== Upload =====================

def upload_mods(cfg: dict):
    banner("Sync Mods - Upload")

    if not cfg.get("mod_folder"):
        print("\nERROR: Mod folder is not set. Use 'Set Mod Folder' first.")
        return pause()
    if not cfg.get("github_repo"):
        print("\nERROR: GitHub repo is not set. Use 'Set Github' first.")
        return pause()
    if not cfg.get("github_token"):
        print("\nERROR: A GitHub token is required for uploads.")
        print("Use 'Set Github' to provide a Personal Access Token.")
        return pause()

    mod_folder = Path(cfg["mod_folder"])
    if not mod_folder.exists():
        print(f"\nERROR: Mod folder does not exist: {mod_folder}")
        return pause()

    base = set(load_base())
    if not base:
        print("\nWARNING: No base set. Every mod in the folder will be uploaded.")
        if not confirm("Continue anyway?"):
            return

    current = set(list_jars(mod_folder))
    target_added = sorted(current - base)        # extras beyond base
    target_removed = sorted(base - current)      # base mods that should be removed

    print()
    print(f"  Base mods:                    {len(base)}")
    print(f"  Current mods in folder:       {len(current)}")
    print(f"  -> Extras to sync (added):    {len(target_added)}")
    print(f"  -> Base removals to sync:     {len(target_removed)}")

    gh = GitHub(cfg["github_repo"], cfg["github_token"], cfg.get("github_branch", "main"))

    print("\nVerifying GitHub access...")
    if not gh.check_repo_access():
        print("ERROR: Couldn't access the repository. Check repo name and token.")
        return pause()

    print("Reading repository state...")
    try:
        repo_added, repo_removed, versions = repo_state(gh)
    except GitHubError as e:
        print(f"ERROR: {e}")
        return pause()

    repo_added_s = set(repo_added)
    repo_removed_s = set(repo_removed)
    target_added_s = set(target_added)
    target_removed_s = set(target_removed)

    new_jars = sorted(target_added_s - repo_added_s)            # need to upload .jar
    drop_added = sorted(repo_added_s - target_added_s)          # added before, no longer wanted
    new_removals = sorted(target_removed_s - repo_removed_s)    # newly removed base mods
    drop_removals = sorted(repo_removed_s - target_removed_s)   # base mods to bring back

    if not (new_jars or drop_added or new_removals or drop_removals):
        print("\nNothing to upload - the repo is already in sync.")
        return pause()

    print(f"\nDelta vs. repo (will become version {next_version(versions)}):")
    if new_jars:
        print(f"  + Upload {len(new_jars)} new mod jar(s):")
        for n in new_jars[:10]:
            print(f"      {n}")
        if len(new_jars) > 10:
            print(f"      ... and {len(new_jars) - 10} more")
    if drop_added:
        print(f"  - Mark {len(drop_added)} previously-added mod(s) for deletion")
    if new_removals:
        print(f"  - Mark {len(new_removals)} additional base mod(s) for deletion")
    if drop_removals:
        print(f"  + Restore {len(drop_removals)} base mod(s)")

    if not confirm("\nProceed with upload?", default_yes=True):
        return

    new_v = next_version(versions)
    print(f"\nUploading version {new_v}...")

    uploaded_jars = []
    for jar in new_jars:
        src = mod_folder / jar
        if not src.exists():
            print(f"  ! Skipping {jar} (not found in folder)")
            continue
        try:
            data = src.read_bytes()
        except OSError as e:
            print(f"  ! Could not read {jar}: {e}")
            continue
        print(f"  > Uploading {jar} ({len(data) / (1024*1024):.1f} MB)...")
        try:
            gh.put_file(
                f"mods/{new_v}/{jar}",
                data,
                f"ModSync: add {jar} ({new_v})",
            )
            uploaded_jars.append(jar)
        except GitHubError as e:
            print(f"    ERROR: {e}")

    manifest = {
        "version": new_v,
        "target_added": sorted(target_added),
        "target_removed": sorted(target_removed),
        "new_jars": sorted(uploaded_jars),
        "changes": {
            "added": new_jars,
            "no_longer_added": drop_added,
            "newly_removed": new_removals,
            "no_longer_removed": drop_removals,
        },
    }
    print("  > Uploading manifest.json...")
    try:
        gh.put_file(
            f"mods/{new_v}/manifest.json",
            json.dumps(manifest, indent=2).encode("utf-8"),
            f"ModSync: manifest for {new_v}",
        )
    except GitHubError as e:
        print(f"    ERROR uploading manifest: {e}")
        return pause()

    h = load_history()
    h["last_synced_version"] = new_v
    save_history(h)

    print(f"\nDone. Repository is now at {new_v}.")
    pause()


# ===================== Download =====================

def download_mods(cfg: dict):
    banner("Sync Mods - Download")

    if not cfg.get("mod_folder"):
        print("\nERROR: Mod folder is not set. Use 'Set Mod Folder' first.")
        return pause()
    if not cfg.get("github_repo"):
        print("\nERROR: GitHub repo is not set. Use 'Set Github' first.")
        return pause()

    mod_folder = Path(cfg["mod_folder"])
    if not mod_folder.exists():
        print(f"\nMod folder does not exist: {mod_folder}")
        if confirm("Create it?", default_yes=True):
            mod_folder.mkdir(parents=True, exist_ok=True)
        else:
            return

    gh = GitHub(cfg["github_repo"], cfg.get("github_token", ""),
                cfg.get("github_branch", "main"))

    print("\nReading repository state...")
    try:
        versions = list_versions(gh)
    except GitHubError as e:
        print(f"ERROR: {e}")
        return pause()

    if not versions:
        print("\nNo version folders in the repository. Nothing to download.")
        return pause()

    print(f"Found {len(versions)} version(s): {', '.join(versions)}")
    print(f"Reading latest manifest ({versions[-1]})...")
    try:
        latest = load_manifest(gh, versions[-1])
    except GitHubError as e:
        print(f"ERROR: {e}")
        return pause()

    target_added = set(latest.get("target_added", []))
    target_removed = set(latest.get("target_removed", []))

    base = set(load_base())
    current = set(list_jars(mod_folder))

    # The desired contents of the mod folder for this user
    desired = (base | target_added) - target_removed

    to_add = sorted(desired - current)
    to_remove = sorted(current - desired)

    if not (to_add or to_remove):
        print("\nAlready in sync with the latest version.")
        h = load_history()
        h["last_synced_version"] = versions[-1]
        save_history(h)
        return pause()

    print("\nSync plan:")
    if to_add:
        print(f"  Download / install: {len(to_add)}")
        for m in to_add[:10]:
            print(f"      + {m}")
        if len(to_add) > 10:
            print(f"      ... and {len(to_add) - 10} more")
    if to_remove:
        print(f"  Remove from folder: {len(to_remove)}")
        for m in to_remove[:10]:
            print(f"      - {m}")
        if len(to_remove) > 10:
            print(f"      ... and {len(to_remove) - 10} more")

    if not confirm("\nProceed?", default_yes=True):
        return

    # Build a map mod_name -> version that physically contains it.
    # This is what handles the "late joiner" case from the spec.
    print("\nLocating mod files across versions...")
    try:
        locations = build_jar_location_map(gh, versions)
    except GitHubError as e:
        print(f"ERROR: {e}")
        return pause()

    # 1) Remove unwanted mods first
    for jar in to_remove:
        p = mod_folder / jar
        if p.exists():
            try:
                p.unlink()
                print(f"  - Removed {jar}")
            except OSError as e:
                print(f"  ! Could not remove {jar}: {e}")

    # 2) Download missing mods
    failed = []
    for jar in to_add:
        v = locations.get(jar)
        if not v:
            print(f"  ! {jar} is not present in any version folder; skipping")
            failed.append(jar)
            continue
        print(f"  + Downloading {jar} from {v}...")
        try:
            data = gh.get_file(f"mods/{v}/{jar}")
        except GitHubError as e:
            print(f"      ERROR: {e}")
            failed.append(jar)
            continue
        if data is None:
            print(f"      ERROR: file not found in repo")
            failed.append(jar)
            continue
        try:
            (mod_folder / jar).write_bytes(data)
        except OSError as e:
            print(f"      ERROR writing file: {e}")
            failed.append(jar)

    h = load_history()
    h["last_synced_version"] = versions[-1]
    save_history(h)

    print(f"\nDone. Now synced to {versions[-1]}.")
    if failed:
        print(f"  {len(failed)} item(s) failed; see warnings above.")
    pause()


# ===================== Top-level menus =====================

def menu_sync(cfg: dict):
    while True:
        clear_screen()
        banner("Sync Mods")
        print()
        print("  1. Upload  (push your changes to GitHub)")
        print("  2. Download (pull changes from GitHub)")
        print("  3. Back")
        print()
        choice = ask("Choose an option")
        if choice == "1":
            upload_mods(cfg)
        elif choice == "2":
            download_mods(cfg)
        elif choice in ("3", "b", "back", ""):
            return
        else:
            print("Invalid option.")
            pause()


def menu_set_base(cfg: dict):
    clear_screen()
    banner("Set Base")
    if not cfg.get("mod_folder"):
        print("\nERROR: Mod folder is not set. Use 'Set Mod Folder' first.")
        return pause()
    folder = Path(cfg["mod_folder"])
    if not folder.exists():
        print(f"\nERROR: Mod folder does not exist: {folder}")
        return pause()

    jars = list_jars(folder)
    print(f"\nFound {len(jars)} .jar file(s) in:\n  {folder}")
    print("\nThese will be set as the BASE - they will be ignored by sync.")
    print("Use this right after a fresh CurseForge modpack install, before")
    print("you add or remove anything.\n")
    for j in jars[:20]:
        print(f"  - {j}")
    if len(jars) > 20:
        print(f"  ... and {len(jars) - 20} more")

    if confirm("\nSet these as the base?", default_yes=True):
        save_base(jars)
        print(f"\nBase saved ({len(jars)} mods).")
    pause()


def menu_set_mod_folder(cfg: dict) -> dict:
    clear_screen()
    banner("Set Mod Folder")
    print(f"\nCurrent: {cfg.get('mod_folder') or '(not set)'}")
    raw = ask("\nPaste the path to your mods folder (or leave empty to cancel)")
    if not raw:
        return cfg
    raw = raw.strip().strip('"').strip("'")
    folder = Path(os.path.expandvars(raw)).expanduser()
    if not folder.exists():
        print(f"\nWARNING: Path does not exist: {folder}")
        if not confirm("Save anyway?"):
            return cfg
    elif not folder.is_dir():
        print(f"\nERROR: Path is not a directory: {folder}")
        pause()
        return cfg
    cfg["mod_folder"] = str(folder)
    save_config(cfg)
    print(f"\nMod folder set to: {folder}")
    pause()
    return cfg


def menu_set_github(cfg: dict) -> dict:
    clear_screen()
    banner("Set GitHub")
    print(f"\nCurrent repo:   {cfg.get('github_repo') or '(not set)'}")
    print(f"Current branch: {cfg.get('github_branch') or 'main'}")
    print(f"Token:          {'(set)' if cfg.get('github_token') else '(not set)'}")
    print()
    print("Repo format: owner/repo  (a full https URL is also accepted)")
    repo = ask("Enter repository (empty = keep current)")
    if repo:
        repo = repo.strip().rstrip("/")
        if repo.startswith("http"):
            # Parse owner/repo out of a URL like https://github.com/user/repo[.git]
            repo = repo.replace(".git", "")
            parts = repo.split("/")
            if len(parts) >= 2:
                repo = f"{parts[-2]}/{parts[-1]}"
        cfg["github_repo"] = repo

    branch = ask(f"Enter branch (empty = keep '{cfg.get('github_branch') or 'main'}')")
    if branch:
        cfg["github_branch"] = branch.strip()
    if not cfg.get("github_branch"):
        cfg["github_branch"] = "main"

    print()
    print("To upload, you need a GitHub Personal Access Token (PAT).")
    print("Create one at: https://github.com/settings/tokens")
    print('Required scope: "repo"  (or "public_repo" if your repo is public).')
    print("The token is stored locally in your ModSync config folder.")
    print("Leave empty to keep the current value (or to remain download-only")
    print("on a public repo).")
    token = ask("Enter token")
    if token:
        cfg["github_token"] = token.strip()

    save_config(cfg)

    if cfg.get("github_repo"):
        print("\nVerifying access...")
        gh = GitHub(cfg["github_repo"], cfg.get("github_token", ""),
                    cfg.get("github_branch", "main"))
        if gh.check_repo_access():
            print("OK - repository is reachable.")
        else:
            print("Could not access repo. It may be private (need a token),")
            print("the repo name may be wrong, or the token may be invalid.")
    pause()
    return cfg


def main_menu():
    cfg = load_config()
    while True:
        clear_screen()
        banner(f"ModSync v{VERSION}")
        print()
        print(f"  Mod folder : {cfg.get('mod_folder') or '(not set)'}")
        repo = cfg.get("github_repo") or "(not set)"
        branch = cfg.get("github_branch") or "main"
        print(f"  GitHub     : {repo}  [{branch}]")
        print(f"  Token      : {'set' if cfg.get('github_token') else 'not set'}")
        print(f"  Base mods  : {len(load_base())}")
        h = load_history()
        if h.get("last_synced_version"):
            print(f"  Last sync  : {h['last_synced_version']}")
        print()
        print("  1. Sync mods")
        print("  2. Set base")
        print("  3. Set mod folder")
        print("  4. Set Github")
        print("  5. Exit")
        print()
        choice = ask("Choose an option")
        if choice == "1":
            menu_sync(cfg)
        elif choice == "2":
            menu_set_base(cfg)
        elif choice == "3":
            cfg = menu_set_mod_folder(cfg)
        elif choice == "4":
            cfg = menu_set_github(cfg)
        elif choice in ("5", "q", "quit", "exit"):
            print("\nGoodbye.")
            return
        else:
            print("Invalid option.")
            pause()


def main():
    try:
        main_menu()
    except KeyboardInterrupt:
        print("\n\nInterrupted.")
        sys.exit(0)


if __name__ == "__main__":
    main()