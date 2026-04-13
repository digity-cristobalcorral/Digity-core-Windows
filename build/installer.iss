; ────────────────────────────────────────────────────────────────────────────
;  Digity Core — Windows Installer
;  Requires: Inno Setup 6+  (https://jrsoftware.org/isinfo.php)
;
;  DO NOT compile this manually.
;  Run  build\build_windows.bat  — it prepares dist\ and calls this script.
;
;  The resulting installer is fully self-contained:
;    • Python 3.11 runtime (bundled — no Python needed on client machine)
;    • All Python packages pre-installed
;    • Client just runs the .exe and clicks Next
; ────────────────────────────────────────────────────────────────────────────

#define AppName      "Digity Core"
#define AppVersion   "1.0.0"
#define AppPublisher "Digity"
#define AppURL       "https://digity.com"

; ── Setup ────────────────────────────────────────────────────────────────────
[Setup]
AppId={{F3A2B891-5C74-4D2E-8A01-BC9E34F76D52}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}

; Install to Program Files by default; allow per-user install too
DefaultDirName={autopf}\DigityCore
DefaultGroupName={#AppName}
PrivilegesRequiredOverridesAllowed=dialog
AllowNoIcons=no

; Output
OutputDir=output
OutputBaseFilename=DigityCore-Setup-{#AppVersion}
; SetupIconFile=..\assets\icon.ico   ; uncomment if you have an icon

; Maximum compression — important given the Python runtime size
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
MinVersion=10.0

; ── Languages ─────────────────────────────────────────────────────────────────
[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

; ── Optional tasks ────────────────────────────────────────────────────────────
[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
    GroupDescription: "Additional icons:"; Flags: unchecked

; ── Files ─────────────────────────────────────────────────────────────────────
[Files]
; Bundled Python runtime + all packages — no Python needed on client machine
Source: "dist\python\*";    DestDir: "{app}\python";    Flags: ignoreversion recursesubdirs

; Project source files
Source: "dist\*.py";        DestDir: "{app}";           Flags: ignoreversion
Source: "dist\*.txt";       DestDir: "{app}";           Flags: ignoreversion
Source: "dist\app\*";       DestDir: "{app}\app";       Flags: ignoreversion recursesubdirs
Source: "dist\core\*";      DestDir: "{app}\core";      Flags: ignoreversion recursesubdirs
Source: "dist\producer\*";  DestDir: "{app}\producer";  Flags: ignoreversion recursesubdirs
Source: "dist\tools\*";     DestDir: "{app}\tools";     Flags: ignoreversion recursesubdirs

; Windows launcher
Source: "dist\launch.bat";  DestDir: "{app}";           Flags: ignoreversion

; ── (Optional) USB drivers ────────────────────────────────────────────────────
; Download CH340 driver from https://www.wch-ic.com/products/CH341.html
; Place CH341SER.EXE in build\prereqs\ and uncomment these lines:
; Source: "prereqs\CH341SER.EXE"; DestDir: "{tmp}"; Flags: deleteafterinstall

; ── Directories ───────────────────────────────────────────────────────────────
[Dirs]
; Create writable subdirectories the app needs at runtime
Name: "{app}\logs"
Name: "{app}\tmp\locks"
; Default data directory on Windows
Name: "{userappdata}\GloveCore\data\session"

; ── Shortcuts ─────────────────────────────────────────────────────────────────
[Icons]
Name: "{group}\{#AppName}"; \
    Filename: "{app}\launch.bat"; WorkingDir: "{app}"; \
    Comment: "Open the Digity Core dashboard"

Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

Name: "{commondesktop}\{#AppName}"; \
    Filename: "{app}\launch.bat"; WorkingDir: "{app}"; \
    Tasks: desktopicon; Comment: "Open the Digity Core dashboard"

; ── Post-install actions ──────────────────────────────────────────────────────
[Run]
; Optional: install CH340 USB driver (uncomment if bundling)
; Filename: "{tmp}\CH341SER.EXE"; \
;     StatusMsg: "Installing USB serial driver..."; \
;     Flags: waituntilterminated

; Offer to launch the app immediately after install
Filename: "{app}\launch.bat"; \
    Description: "Launch {#AppName} now"; \
    Flags: postinstall nowait skipifsilent unchecked
