#define AppName "RAM Cutter"
#define AppExeName "RAM Cutter.exe"

#ifndef AppVersion
  #error AppVersion must be provided by build.bat from pyproject.toml
#endif

[Setup]
AppId={{A461F811-D356-4B7A-8A5C-9176FD36FC2B}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=RAM Cutter
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=RAM Cutter v{#AppVersion} Setup
SetupIconFile=..\assets\RAM Cutter.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
VersionInfoDescription=RAM Cutter Setup
VersionInfoProductName=RAM Cutter
VersionInfoProductVersion={#AppVersion}
VersionInfoVersion={#AppVersion}.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startmenuicon"; Description: "Create a Start Menu shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce
Name: "desktopicon"; Description: "Create a Desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce

[Files]
Source: "..\dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"; Tasks: startmenuicon
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch RAM Cutter"; Flags: postinstall nowait skipifsilent