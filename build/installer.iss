; ────────────────────────────────────────────────────────────────────────────
;  Digity Core — Windows Installer
;  Requires: Inno Setup 6+  (https://jrsoftware.org/isinfo.php)
;
;  HOW TO BUILD
;  ────────────
;  1. Copy this entire project directory to a Windows machine.
;  2. Download Python 3.11.x from https://python.org/downloads/
;     and save it as:  build\prereqs\python-3.11.9-amd64.exe
;     (The installer is bundled so clients don't need internet access.)
;  3. (Optional) Download the CH340 driver from:
;     https://www.wch-ic.com/products/CH341.html  →  CH341SER.EXE
;     and save as:  build\prereqs\CH341SER.EXE
;  4. Open this file in the Inno Setup IDE and click Build → Compile,
;     or run from the command line:
;       "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
;  5. The installer is written to:  build\output\DigityCore-Setup-1.0.0.exe
; ────────────────────────────────────────────────────────────────────────────

#define AppName      "Digity Core"
#define AppVersion   "1.0.0"
#define AppPublisher "Digity"
#define AppURL       "https://digity.com"

; ── Setup metadata ────────────────────────────────────────────────────────────
[Setup]
AppId={{F3A2B891-5C74-4D2E-8A01-BC9E34F76D52}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}

; Install per-user by default (no UAC needed), but allow system-wide
DefaultDirName={autopf}\DigityCore
DefaultGroupName={#AppName}
PrivilegesRequiredOverridesAllowed=dialog
AllowNoIcons=no

; Output
OutputDir=output
OutputBaseFilename=DigityCore-Setup-{#AppVersion}
SetupIconFile=..\assets\icon.ico
; UninstallDisplayIcon={app}\assets\icon.ico

; Compression
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern

; Windows 10 minimum
MinVersion=10.0

; ── Languages ─────────────────────────────────────────────────────────────────
[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

; ── Optional tasks ────────────────────────────────────────────────────────────
[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
    GroupDescription: "Additional icons:"; Flags: unchecked

; ── Files to install ──────────────────────────────────────────────────────────
[Files]
; Core Python source
Source: "..\main.py";           DestDir: "{app}";              Flags: ignoreversion
Source: "..\requirements.txt";  DestDir: "{app}";              Flags: ignoreversion
Source: "..\app\*";             DestDir: "{app}\app";          Flags: ignoreversion recursesubdirs
Source: "..\core\*";            DestDir: "{app}\core";         Flags: ignoreversion recursesubdirs
Source: "..\producer\*";        DestDir: "{app}\producer";     Flags: ignoreversion recursesubdirs
Source: "..\tools\*";           DestDir: "{app}\tools";        Flags: ignoreversion recursesubdirs

; Build helpers
Source: "launch.bat";           DestDir: "{app}";              Flags: ignoreversion
Source: "setup_venv.py";        DestDir: "{app}";              Flags: ignoreversion

; Bundled Python installer (downloaded in advance — see HOW TO BUILD above)
Source: "prereqs\python-3.11.9-amd64.exe"; DestDir: "{tmp}"; \
    Flags: deleteafterinstall; Check: not PythonInstalled

; Optional: CH340 USB-serial driver (comment out if not bundling)
; Source: "prereqs\CH341SER.EXE"; DestDir: "{tmp}"; Flags: deleteafterinstall

; ── Directories ───────────────────────────────────────────────────────────────
[Dirs]
Name: "{app}\logs"
Name: "{app}\tmp\locks"
Name: "{userappdata}\GloveCore\data\session"

; ── Shortcuts ─────────────────────────────────────────────────────────────────
[Icons]
; Start Menu
Name: "{group}\{#AppName}"; \
    Filename: "{app}\launch.bat"; WorkingDir: "{app}"; \
    Comment: "Open the Digity Core dashboard"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

; Desktop (optional task)
Name: "{commondesktop}\{#AppName}"; \
    Filename: "{app}\launch.bat"; WorkingDir: "{app}"; \
    Tasks: desktopicon; Comment: "Open the Digity Core dashboard"

; ── Post-install commands ─────────────────────────────────────────────────────
[Run]
; 1. Install Python 3.11 silently (per-user, added to PATH)
Filename: "{tmp}\python-3.11.9-amd64.exe"; \
    Parameters: "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=0"; \
    StatusMsg: "Installing Python 3.11..."; \
    Flags: waituntilterminated; Check: not PythonInstalled

; 2. (Optional) Install CH340 USB driver
; Filename: "{tmp}\CH341SER.EXE"; \
;     StatusMsg: "Installing USB serial driver..."; \
;     Flags: waituntilterminated

; 3. Create virtualenv + install all Python packages
Filename: "{code:GetPythonExe}"; \
    Parameters: """{app}\setup_venv.py"""; \
    WorkingDir: "{app}"; \
    StatusMsg: "Installing Python packages (may take a few minutes)..."; \
    Flags: waituntilterminated

; 4. Offer to launch immediately after install
Filename: "{app}\launch.bat"; \
    Description: "Launch {#AppName} now"; \
    Flags: postinstall nowait skipifsilent unchecked

; ── Inno Setup Pascal script ──────────────────────────────────────────────────
[Code]

{ Check whether Python 3.10, 3.11, or 3.12 is already installed. }
function PythonInstalled: Boolean;
var
  S: String;
begin
  Result :=
    RegQueryStringValue(HKCU, 'Software\Python\PythonCore\3.12\InstallPath', '', S) or
    RegQueryStringValue(HKLM, 'Software\Python\PythonCore\3.12\InstallPath', '', S) or
    RegQueryStringValue(HKCU, 'Software\Python\PythonCore\3.11\InstallPath', '', S) or
    RegQueryStringValue(HKLM, 'Software\Python\PythonCore\3.11\InstallPath', '', S) or
    RegQueryStringValue(HKCU, 'Software\Python\PythonCore\3.10\InstallPath', '', S) or
    RegQueryStringValue(HKLM, 'Software\Python\PythonCore\3.10\InstallPath', '', S);
end;

{ Return the path to python.exe.  Searches registry and common locations. }
function GetPythonExe(Param: String): String;
var
  InstallPath: String;
  Candidates: TStringList;
  I: Integer;
begin
  { Try registry first (per-user install) }
  if RegQueryStringValue(HKCU, 'Software\Python\PythonCore\3.12\InstallPath', '', InstallPath) or
     RegQueryStringValue(HKLM, 'Software\Python\PythonCore\3.12\InstallPath', '', InstallPath) or
     RegQueryStringValue(HKCU, 'Software\Python\PythonCore\3.11\InstallPath', '', InstallPath) or
     RegQueryStringValue(HKLM, 'Software\Python\PythonCore\3.11\InstallPath', '', InstallPath) or
     RegQueryStringValue(HKCU, 'Software\Python\PythonCore\3.10\InstallPath', '', InstallPath) or
     RegQueryStringValue(HKLM, 'Software\Python\PythonCore\3.10\InstallPath', '', InstallPath)
  then begin
    Result := InstallPath + 'python.exe';
    if FileExists(Result) then Exit;
  end;

  { Fallback: common install directories }
  Candidates := TStringList.Create;
  try
    Candidates.Add(ExpandConstant('{localappdata}\Programs\Python\Python312\python.exe'));
    Candidates.Add(ExpandConstant('{localappdata}\Programs\Python\Python311\python.exe'));
    Candidates.Add(ExpandConstant('{localappdata}\Programs\Python\Python310\python.exe'));
    Candidates.Add('C:\Python312\python.exe');
    Candidates.Add('C:\Python311\python.exe');
    Candidates.Add('C:\Python310\python.exe');
    for I := 0 to Candidates.Count - 1 do
      if FileExists(Candidates[I]) then begin
        Result := Candidates[I];
        Exit;
      end;
  finally
    Candidates.Free;
  end;

  { Last resort: hope it's on PATH }
  Result := 'python.exe';
end;
