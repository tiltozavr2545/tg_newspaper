; TG Newspaper installer for Windows (Inno Setup 6).
; Build: iscc /DAppVersion=0.2.0 /DStageDir=<directory with sources> packaging\windows\tg-newspaper.iss
; Result: Output\TG-Newspaper-<version>-windows-setup.exe (the directory can be changed with /O<directory>).
;
; StageDir is prepared by CI (git archive of only the project files that are needed: pyproject.toml,
; uv.lock, README.md, .python-version, src\, scripts\console.py, scripts\preparing.html) —
; this way secrets and working-copy clutter don't end up in the installer.
; The installer contains only the program; Python, dependencies and Chromium are
; installed by launch.ps1 on first run into %LOCALAPPDATA%\TG Newspaper.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef StageDir
  #define StageDir "..\..\build\stage"
#endif

[Setup]
; Fixed GUID: a new version installs over the previous one as an update.
AppId={{6B0D3F2A-4C1E-4E8B-9A57-7D2F1C8E5A31}
AppName=TG Newspaper
AppVersion={#AppVersion}
AppPublisher=TG Newspaper
; No administrator rights needed: we install into the user profile.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\TG Newspaper
DefaultGroupName=TG Newspaper
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\app.ico
SetupIconFile=..\icons\app.ico
OutputBaseFilename=TG-Newspaper-{#AppVersion}-windows-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
; A desktop shortcut is offered and enabled by default.
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Upgrading in place: remove old sources so no deleted modules are left behind.
Type: filesandordirs; Name: "{app}\src"
Type: filesandordirs; Name: "{app}\scripts"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "launch.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "launch.vbs"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\icons\app.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; wscript + launch.vbs launch PowerShell hidden - no flashing window.
Name: "{autoprograms}\TG Newspaper"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\launch.vbs"""; IconFilename: "{app}\app.ico"; WorkingDir: "{app}"
Name: "{autodesktop}\TG Newspaper"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\launch.vbs"""; IconFilename: "{app}\app.ico"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{sys}\wscript.exe"; Parameters: """{app}\launch.vbs"""; WorkingDir: "{app}"; Description: "Запустить TG Newspaper"; Flags: postinstall nowait skipifsilent

; The uninstaller intentionally does NOT declare [UninstallDelete] for %LOCALAPPDATA%\TG Newspaper:
; it holds settings, the Telegram session, the issue database, the venv and the browser - user data
; that must not be lost when the program is removed or reinstalled. It can be deleted manually.
