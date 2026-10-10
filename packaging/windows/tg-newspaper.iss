; Установщик TG Newspaper для Windows (Inno Setup 6).
; Сборка: iscc /DAppVersion=0.2.0 /DStageDir=<каталог с исходниками> packaging\windows\tg-newspaper.iss
; Результат: Output\TG-Newspaper-<версия>-windows-setup.exe (каталог можно сменить /O<каталог>).
;
; StageDir готовит CI (git archive только нужных файлов проекта: pyproject.toml,
; uv.lock, README.md, .python-version, src\, scripts\console.py, scripts\preparing.html) —
; так секреты и мусор рабочей копии в установщик не попадают.
; В установщике только программа; Python, зависимости и Chromium launch.ps1
; ставит при первом запуске в %LOCALAPPDATA%\TG Newspaper.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef StageDir
  #define StageDir "..\..\build\stage"
#endif

[Setup]
; Фиксированный GUID: новая версия ставится поверх прежней как обновление.
AppId={{6B0D3F2A-4C1E-4E8B-9A57-7D2F1C8E5A31}
AppName=TG Newspaper
AppVersion={#AppVersion}
AppPublisher=TG Newspaper
; Без прав администратора: ставим в профиль пользователя.
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
; Ярлык на рабочем столе предлагается и включён по умолчанию.
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Обновление поверх: убираем старые исходники, чтобы не остались удалённые модули.
Type: filesandordirs; Name: "{app}\src"
Type: filesandordirs; Name: "{app}\scripts"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "launch.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "launch.vbs"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\icons\app.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; wscript + launch.vbs запускают PowerShell скрыто - без мелькающего окна.
Name: "{autoprograms}\TG Newspaper"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\launch.vbs"""; IconFilename: "{app}\app.ico"; WorkingDir: "{app}"
Name: "{autodesktop}\TG Newspaper"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\launch.vbs"""; IconFilename: "{app}\app.ico"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{sys}\wscript.exe"; Parameters: """{app}\launch.vbs"""; WorkingDir: "{app}"; Description: "Запустить TG Newspaper"; Flags: postinstall nowait skipifsilent

; Деинсталлятор намеренно НЕ описывает [UninstallDelete] для %LOCALAPPDATA%\TG Newspaper:
; там настройки, сессия Telegram, база выпусков, venv и браузер - данные пользователя
; не должны пропадать при удалении или переустановке программы. Удалить их можно вручную.
