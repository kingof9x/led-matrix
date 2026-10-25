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
    'yaml', 'pynput', 'requests', 'zoneinfo', 'iplocate', 'dotenv',
    'scipy', 'scipy._distributor_init', 'scipy._lib.messagestream', 'scipy.special._ufuncs',
    'scipy.linalg._flapack', 'scipy.signal',
    # Plugins are loaded from files at runtime, so PyInstaller can't see what they import
    'led_mon.patterns', 'led_mon.drawing', 'led_mon.shared_state', 'led_mon.framework_tool',
    'led_mon.equalizer_files.visualize',
    # The layout editor is imported only when started with --edit-layout
    'led_mon.rendering', 'led_mon.control', 'led_mon.editor', 'led_mon.editor.window', 'led_mon.editor.preview',
    'led_mon.editor.layout', 'led_mon.editor.catalog', 'tkinter', 'tkinter.ttk', 'tkinter.messagebox',
]

icon = None
if IS_WINDOWS:
    hiddenimports += [
        'soundcard', 'pystray', 'pystray._win32', 'PIL', 'PIL.ImageTk', 'wmi', 'win32com', 'win32com.client', 'pythoncom',
        'pynput.keyboard._win32', 'pynput.mouse._win32', 'serial.tools.list_ports_windows', 'tzdata',
    ]
    # soundcard reads its C header at runtime; zoneinfo needs the tzdata database on Windows
    datas += collect_data_files('soundcard') + collect_data_files('tzdata')
    # Build the exe icon from the same drawing as the tray icon
    sys.path.insert(0, os.path.abspath('.'))
    from led_mon.tray import make_icon_image
    os.makedirs('build', exist_ok=True)
    icon = os.path.join('build', 'led_mon.ico')
    make_icon_image(256).save(icon, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
else:
    hiddenimports += ['sounddevice', 'pulsectl']

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe_options = dict(
    name='led_mon',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # Windows runs windowless with a tray icon; Linux stays a console app (systemd service)
    console=not IS_WINDOWS,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
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
        upx=True,
        upx_exclude=[],
        name='led_mon',
    )
