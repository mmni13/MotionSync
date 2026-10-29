#ifndef AppVersion
  #error AppVersion must be passed with /DAppVersion=x.y.z
#endif

[Setup]
AppId={{5B8E3142-5C4F-4F22-8C20-1B9A5E9D6827}
AppName=MotionSync
AppVersion={#AppVersion}
AppPublisher=MotionSync
DefaultDirName={localappdata}\Programs\MotionSync
DefaultGroupName=MotionSync
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64
OutputDir=..\release
OutputBaseFilename=MotionSync-Setup-{#AppVersion}-windows-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
Uninstallable=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\MotionSync\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "update_helper.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\MotionSync"; Filename: "{app}\MotionSync.exe"
Name: "{autodesktop}\MotionSync"; Filename: "{app}\MotionSync.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\MotionSync.exe"; Description: "Launch MotionSync"; Flags: nowait postinstall skipifsilent
