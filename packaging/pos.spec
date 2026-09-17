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
