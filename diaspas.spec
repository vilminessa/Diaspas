# -*- mode: python ; coding: utf-8 -*-
# Сборка: pyinstaller diaspas.spec
# Один файл, без консольного окна; Tkinter - из стандартной библиотеки.
#
# Версия exe берётся из version.py (__version__): VersionInfo генерируется в
# временный файл, потому что захардкодить её в спеке нельзя - CI проставляет
# значение из тега (tools/stamp_version.py) перед сборкой. Строки ресурса -
# ASCII: их читает Windows, а не Python.

import os
import sys
import tempfile

block_cipher = None

# PyInstaller даёт SPECPATH (папка спека); локальный прогон без него - cwd.
spec_dir = globals().get("SPECPATH") or os.getcwd()
sys.path.insert(0, spec_dir)
from version import BUILD_LABEL, __version__  # noqa: E402

_components = [int(part) for part in __version__.split(".")]
_v4 = tuple(_components[:4]) + (0,) * (4 - len(_components))

_version_file = os.path.join(tempfile.gettempdir(), "diaspas-version-info.txt")
with open(_version_file, "w", encoding="utf-8") as _fh:
    _fh.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({_v4[0]}, {_v4[1]}, {_v4[2]}, {_v4[3]}),
    prodvers=({_v4[0]}, {_v4[1]}, {_v4[2]}, {_v4[3]}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('FileDescription', 'Diaspas - DPI bypass manager'),
        StringStruct('FileVersion', '{BUILD_LABEL or __version__}'),
        StringStruct('InternalName', 'Diaspas'),
        StringStruct('OriginalFilename', 'Diaspas.exe'),
        StringStruct('ProductName', 'Diaspas'),
        StringStruct('ProductVersion', '{__version__}'),
        StringStruct('CompanyName', 'vilminessa'),
        StringStruct('LegalCopyright', 'Copyright (c) 2026 vilminessa'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        "tkinter",
        "tkinter.ttk",
        "tkinter.filedialog",
        "tkinter.messagebox",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Diaspas",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=_version_file,
)
