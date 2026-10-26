# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Linux and Windows.
#
#   pyinstaller led_mon.spec                          one-dir build: dist/led_mon/
#   LED_MON_ONEFILE=1 pyinstaller led_mon.spec        one-file build: dist/led_mon(.exe)  (portable exe on Windows)

import os
import sys
from PyInstaller.utils.hooks import collect_data_files

IS_WINDOWS = sys.platform == 'win32'
ONEFILE = os.environ.get('LED_MON_ONEFILE', '') not in ('', '0')

datas = [
    (os.path.join('led_mon', 'plugins'), os.path.join('led_mon', 'plugins')),
    (os.path.join('led_mon', 'snapshot_files'), os.path.join('led_mon', 'snapshot_files')),
    (os.path.join('led_mon', 'config.yaml'), 'led_mon'),
    (os.path.join('led_mon', 'equalizer_files'), os.path.join('led_mon', 'equalizer_files')),
]

hiddenimports = [
    'yaml', 'requests', 'zoneinfo', 'iplocate', 'dotenv',
    'scipy', 'scipy._distributor_init', 'scipy._lib.messagestream', 'scipy.special._ufuncs',
    'scipy.linalg._flapack', 'scipy.signal',
    # Plugins are loaded from files at runtime, so PyInstaller can't see what they import
    'led_mon.patterns', 'led_mon.drawing', 'led_mon.shared_state', 'led_mon.framework_tool',
    'led_mon.equalizer_files.visualize',
    'led_mon.hotkeys',
    # The layout editor is imported only when started with --edit-layout
    'led_mon.rendering', 'led_mon.control', 'led_mon.editor', 'led_mon.editor.window', 'led_mon.editor.preview',
    'led_mon.editor.layout', 'led_mon.editor.catalog', 'tkinter', 'tkinter.ttk', 'tkinter.messagebox',
    # Imported when the tray starts
    'led_mon.tray', 'led_mon.brightness_popup', 'led_mon.app_settings', 'led_mon.external_apps', 'led_mon.startup',
]

icon = None
version_info = None
if IS_WINDOWS:
    hiddenimports += [
        'soundcard', 'pystray', 'pystray._win32', 'PIL', 'PIL.ImageTk', 'wmi', 'win32com', 'win32com.client', 'pythoncom',
        'serial.tools.list_ports_windows', 'tzdata',
    ]
    # soundcard reads its C header at runtime; zoneinfo needs the tzdata database on Windows
    datas += collect_data_files('soundcard') + collect_data_files('tzdata')
    # Build the exe icon from the same drawing as the tray icon
    sys.path.insert(0, os.path.abspath('.'))
    from led_mon.tray import make_icon_image
    os.makedirs('build', exist_ok=True)
    icon = os.path.join('build', 'led_mon.ico')
    make_icon_image(256).save(icon, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    # Version details shown in the exe's Properties; an exe without them looks more suspicious to scanners
    import tomllib
    from PyInstaller.utils.win32.versioninfo import (VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable,
                                                     StringStruct, VarFileInfo, VarStruct)
    with open('pyproject.toml', 'rb') as f:
        app_version = tomllib.load(f)['project']['version']
    version_tuple = tuple(int(p) for p in (app_version.split('.') + ['0'] * 4)[:4])
    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=version_tuple, prodvers=version_tuple),
        kids=[
            StringFileInfo([StringTable('040904B0', [
                StringStruct('CompanyName', 'led-matrix contributors'),
                StringStruct('FileDescription', 'LED Matrix Monitor for the Framework 16'),
                StringStruct('FileVersion', app_version),
                StringStruct('InternalName', 'led_mon'),
                StringStruct('LegalCopyright', 'MIT License'),
                StringStruct('OriginalFilename', 'led_mon.exe'),
                StringStruct('ProductName', 'LED Matrix Monitor'),
                StringStruct('ProductVersion', app_version),
            ])]),
            VarFileInfo([VarStruct('Translation', [1033, 1200])]),
        ],
    )
else:
    hiddenimports += ['pynput', 'sounddevice', 'pulsectl']

# Windows uses registered hotkeys; keep pynput's keyboard hook out of the exe (antivirus flags it)
excludes = ['pynput'] if IS_WINDOWS else []

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe_options = dict(
    name='led_mon',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # No UPX: antivirus scanners treat UPX-packed executables as suspicious
    upx=False,
    # Windows runs windowless with a tray icon; Linux stays a console app (systemd service)
    console=not IS_WINDOWS,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
    version=version_info,
)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        upx_exclude=[],
        runtime_tmpdir=None,
        **exe_options,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        **exe_options,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        # No UPX: antivirus scanners treat UPX-packed executables as suspicious
    upx=False,
        upx_exclude=[],
        name='led_mon',
    )
