# The packaged build — closing phase 6

Phase 6 is not done until it **runs from the packaged build, not the dev
environment** (execution plan §5). This is the guide for that box, and for the
one under it: *a non-developer has used it for 15 minutes*.

## What this is not

The architecture puts `packaging/{pos.spec, installer.iss, version.json}` in
**phase 9**, along with code signing, the Inno Setup installer, the signed
`version.json` manifest, background updates and antivirus reputation
submission. None of that belongs here.

What phase 6 needs is the smallest honest version: a one-folder PyInstaller
build you can copy to a machine and use. No installer, no signing, no updater.
`packaging/pos.spec` is the one artefact that survives into phase 9 — that
phase grows it rather than replacing it.

The point of the exercise is not the exe. It is that **a frozen build fails
differently from a dev run**, and those differences are invisible until you
look. Three of them are already sitting in this codebase, named below.

---

## Prerequisites

A Windows machine. PyInstaller emits a binary for the platform it runs on, so
a Windows `pos.exe` must be built on Windows — the Linux laptop cannot do it,
and neither can a Linux VM with the folder mounted.

```powershell
git pull                     # make sure you are at origin/main
python -m pip install pyinstaller
```

Check the webview backend is present, because a missing one fails at window
creation rather than at build time:

```powershell
python -c "import webview, clr_loader, importlib.metadata as m; print(m.version('pywebview'))"
```

(`webview.__version__` does not exist in pywebview 6.x, so asking for it
raises `AttributeError` on a machine where the backend is in fact fine.)

An `ImportError` on `clr_loader` means `pip install pythonnet` — pywebview's
Windows backend is EdgeChromium via .NET, and WebView2 must be installed on
the machine (it ships with Windows 11 and current Windows 10).

---

## Step 1 — Build the UI first

The packaged app serves whatever is in `app/ui/`. That directory is build
output, ignored by git, and it has already gone six days stale once in this
project — long enough that a whole feature looked missing.

```powershell
cd ui-src
npm run build
cd ..
```

`npm run build` runs `tsc -b` before Vite, so this is also your typecheck.

Confirm it is actually fresh:

```powershell
Get-ChildItem app\ui\assets | Select-Object Name, LastWriteTime
```

If that timestamp is not from the last few minutes, stop. Everything below
will package the old bundle perfectly.

---

## Step 2 — Write the spec file

Save as `packaging/pos.spec`:

```python
# -*- mode: python ; coding: utf-8 -*-
"""One-folder build for the phase 6 definition of done.

The two `datas` entries are the whole reason this file exists. `server.py`
computes the UI directory as `Path(__file__).parent.parent / "ui"` and
`migrations.py` computes its directory as `Path(__file__).parent /
"migrations"`. PyInstaller gives a frozen module a `__file__` inside the
bundle, so those paths resolve correctly **only if the destinations below
mirror the package layout exactly**. Change `app/ui` to `ui` here and the
window loads blank with no error anywhere.
"""

block_cipher = None

a = Analysis(
    ["../app/main.py"],
    pathex=[".."],
    binaries=[],
    datas=[
        ("../app/ui", "app/ui"),                          # React bundle
        ("../app/data/migrations", "app/data/migrations"), # local schema
    ],
    hiddenimports=[
        "argon2",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="pos",
    debug=False,
    strip=False,
    upx=False,          # UPX and antivirus heuristics do not get along
    console=True,       # keep the console until the build works; see step 6
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, name="pos",
)
```

`upx=False` is deliberate. Compressed binaries are a reliable way to get
quarantined, and phase 9 already budgets for AV reputation without inviting
the problem early.

---

## Step 3 — Build

```powershell
pyinstaller --noconfirm --clean --distpath dist --workpath build packaging\pos.spec
```

`dist/` and `build/` are already in `.gitignore`.

If keyring or reportlab fail to import at runtime, add them to the build
rather than guessing at hidden imports:

```powershell
pyinstaller --noconfirm --clean --collect-all keyring --collect-data reportlab packaging\pos.spec
```

---

## Step 4 — Deal with `.env` before you launch

`Settings` reads `env_file=".env"`, and pydantic-settings resolves that
**relative to the current working directory**, not to the executable. Launched
from a desktop shortcut, the working directory is wherever the shortcut points
— so the Supabase URL and anon key may simply not be there.

The symptom is specific and easy to misread: the till sells fine, and
everything touching the cloud refuses. It looks like a network problem and is
a packaging problem.

Pick one deliberately:

- **Machine environment variables** (recommended for this test). The settings
  use `env_prefix="POS_"`, so `POS_SUPABASE_URL`, `POS_SUPABASE_ANON_KEY`,
  `POS_STORE_CODE`, `POS_TERMINAL_CODE`, `POS_TERMINAL_ID`. No file to
  misplace, and nothing readable in the install folder.
- **Copy `.env` next to `pos.exe`** and launch from that folder. Works, but
  only because the working directory happens to be right.

The durable fix — resolving the env file next to `sys.executable` when frozen
— belongs with the phase 9 packaging work. Write down which you chose;
whoever runs this next will assume the other one.

**Chosen on this machine (2026-09-17): user-scope environment variables.**
`POS_SUPABASE_URL`, `POS_SUPABASE_ANON_KEY`, `POS_STORE_CODE`,
`POS_TERMINAL_CODE`, `POS_TERMINAL_ID` are set at `User` scope, not `Machine`
— same effect for a single-operator test, no elevation, and removable with
`[Environment]::SetEnvironmentVariable($k, $null, 'User')`. There is no `.env`
in the build folder, which is the point: the repo `.env` carries the
`service_role` and Gemini keys, so copying it next to `pos.exe` would ship
exactly what the paragraph below forbids. If you take the file route, filter
it to the `POS_` lines first.

Never put the `service_role` key in either. Architecture §1.7: a PyInstaller
bundle decompiles trivially.

---

## Step 5 — Run it from somewhere that is not the repo

```powershell
Copy-Item -Recurse dist\pos $HOME\Desktop\pos-build
$HOME\Desktop\pos-build\pos.exe
```

Running it from inside the checkout hides exactly the bugs this step exists to
find, because the dev copies of `app/ui` and the migrations are sitting right
there on disk.

The database, lock file and logs go to `C:\ProgramData\RetailPOS` — outside
the install directory by design. If the app cannot start and the log is empty,
check that folder is writable by the account you are running as.

### The three things that break, and what each looks like

| Symptom | Cause | Fix |
|---|---|---|
| Window opens, stays blank white | `app/ui` missing or at the wrong path in the bundle | the first `datas` entry, destination spelled `app/ui` |
| "The till could not start" with a migration error on first run | `app/data/migrations/*.sql` not bundled | the second `datas` entry |
| Sells fine, every cloud action refuses | `.env` not found from the working directory | step 4 |

A fourth to watch for: the splash never gives way to the register. That is the
`/health` gate timing out — `health_timeout_seconds` is 20 — and it usually
means uvicorn died in its thread. The console you kept in step 2 will have the
traceback.

---

## Step 6 — Exercise it, then re-check the other boxes

Against the packaged build, not the dev run:

1. Sign in, sell a catalogued item, take cash.
2. Sell something unknown as an unlisted line.
3. Open **Unknown scans**, catalogue it through New product.
4. Sell it again by scanning — it should now resolve.
5. Dismiss a second unknown scan and confirm it asks first.
6. **Disconnect the network and repeat 1–2.** Selling must keep working;
   admin must refuse with a sentence about needing the internet, not a stack
   trace. That is its own definition-of-done box.
7. Check `C:\ProgramData\RetailPOS\logs\pos.log` has rotating output and no
   unhandled exceptions.

Then rebuild with `console=False` in the spec and confirm it still starts. A
window that paints with a console and not without one is a real difference,
not a cosmetic one — and you want to find that now rather than in a shop.

---

## Step 7 — Commit the spec

```powershell
git add packaging\pos.spec docs\packaged-build.md
git commit -m "Package the till for the phase 6 definition of done"
git push
```

---

## Then: fifteen minutes with a non-developer

The last box, and the plan's own note on it: *that one catches more real
defects than any of the others.* This project has the evidence — three defects
in one week, none found by a test suite, all found by someone pressing things.

- **Pick someone who works a counter**, not a technical friend, and who has
  never seen the screen.
- **Give tasks in their language.** "A customer is buying these three things,
  one of them won't scan." Not "test the unknown-scan queue."
- **Sit behind them and do not touch the keyboard.** No demo first. The
  instinct to help is the instinct to destroy the data you came for.
- **Write down the moment, not the opinion.** Where they paused, what they
  read twice, what they clicked that did nothing.
- **Watch for silent failures specifically.** Did the screen just tell them
  something happened that did not? That is the family of bug this codebase
  keeps producing: an edit that never saved, a queue entry closed without
  cataloguing, a stale bundle, an ignore rule made of UTF-16.
- **Stop at fifteen minutes.** Fatigue turns useful confusion into polite
  compliance.
- **Triage the same day.** Each observation becomes a card, a one-line copy
  change, or a deliberate "no". Copy changes are usually the cheapest fix with
  the largest effect — "Save changes" and "Dismiss" both came out of exactly
  this.

## The phase 6 checklist, for reference

- [x] Tests pass in CI, including the permission matrix
- [ ] It runs from the **packaged build**, not the dev environment
- [ ] It works with the network disconnected, or fails with a message a
      cashier can act on
- [x] No new `float` in money paths, no new direct SQLite connections outside
      repositories
- [x] Audit rows exist for anything a manager would need to investigate later
- [ ] A non-developer has used it for 15 minutes
