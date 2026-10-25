; The same installer definition is used by local builds and GitHub Actions.
#if ((VER & 0xFFFFFF00) != EncodeVer(6, 7, 1)) && ((VER & 0xFFFFFF00) != EncodeVer(6, 7, 3))
  #error Only Inno Setup 6.7.1 and 6.7.3 are supported for this build.
#endif
#ifndef AppVersion
  #error AppVersion must be supplied by scripts/build_windows.ps1.
#endif
#ifndef PayloadDir
  #error PayloadDir must be supplied by scripts/build_windows.ps1.
#endif
#ifndef OutputDir
  #error OutputDir must be supplied by scripts/build_windows.ps1.
#endif

[Setup]
AppId={{C7ED0871-6753-4AFA-9BBA-440350FABE41}
AppName=VASL AI Bridge
AppVersion={#AppVersion}
AppVerName=VASL AI Bridge {#AppVersion}
AppPublisher=VASL AI Bridge
DefaultDirName={localappdata}\Programs\VASL AI Bridge
DefaultGroupName=VASL AI Bridge
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=VASL-AI-Bridge-Setup-{#AppVersion}-x64
Compression=zip
SolidCompression=no
WizardStyle=modern
SetupIconFile=..\vasl_ai_bridge\assets\app.ico
UninstallDisplayIcon={app}\VASL AI Bridge.exe
UninstallDisplayName=VASL AI Bridge
CloseApplications=yes
RestartApplications=no
SetupLogging=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs


[Icons]
Name: "{group}\VASL AI Bridge"; Filename: "{app}\VASL AI Bridge.exe"; WorkingDir: "{app}"
Name: "{group}\Uninstall VASL AI Bridge"; Filename: "{uninstallexe}"
Name: "{autodesktop}\VASL AI Bridge"; Filename: "{app}\VASL AI Bridge.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\VASL AI Bridge.exe"; Description: "Launch VASL AI Bridge"; Flags: nowait postinstall skipifsilent unchecked

; No UninstallDelete entries: external engines and application user data are retained.
