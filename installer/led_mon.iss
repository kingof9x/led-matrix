; Inno Setup 6 script for the Windows installer of the Framework 16 LED Matrix monitor.
; Build after the one-dir PyInstaller build (dist\led_mon\):
;   iscc /DAppVersion=2.1.2 installer\led_mon.iss
; Output: dist\led_mon-setup-<version>.exe
;
; The installed app is the same as the portable exe, except that it starts when the user logs in.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F5C3A51-2B7E-4C0B-9E5D-3C1A7F2B8D64}
AppName=LED Matrix Monitor
AppVersion={#AppVersion}
AppPublisher=led-matrix contributors
AppPublisherURL=https://github.com/timoteuszelle/led-matrix
DefaultDirName={localappdata}\Programs\led_mon
DefaultGroupName=LED Matrix Monitor
DisableProgramGroupPage=yes
DisableDirPage=yes
; Per-user install, no admin prompt
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=led_mon-setup-{#AppVersion}
SetupIconFile=..\build\led_mon.ico
UninstallDisplayIcon={app}\led_mon.exe
UninstallDisplayName=LED Matrix Monitor
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Close a running instance before installing or uninstalling. The mutex name matches the app's.
CloseApplications=yes
AppMutex=led_mon_single_instance

[Files]
Source: "..\dist\led_mon\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\LED Matrix Monitor"; Filename: "{app}\led_mon.exe"
Name: "{group}\LED Matrix Layout Editor"; Filename: "{app}\led_mon.exe"; Parameters: "--edit-layout"
Name: "{group}\Uninstall LED Matrix Monitor"; Filename: "{uninstallexe}"

[Registry]
; Start with Windows (can be turned off in Task Manager > Startup apps)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "led_mon"; ValueData: """{app}\led_mon.exe"""; Flags: uninsdeletevalue

[Run]
Filename: "{app}\led_mon.exe"; Description: "Launch led_mon"; Flags: nowait postinstall skipifsilent

; The user's config in %APPDATA%\led_mon is not touched by the uninstaller.
