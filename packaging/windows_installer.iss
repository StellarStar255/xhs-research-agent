; Inno Setup script for the Windows installer (per-user install, no admin rights).
; Built by .github/workflows/release.yml:  iscc /DAppVersion=0.1.0 packaging\windows_installer.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "小红书调研助手"
#define AppExe "XHS Research Agent.exe"

[Setup]
AppId={{6F1C8E2A-3B7D-4E59-9A0C-2D5B8F4E7A31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=xhs-research-agent (open source, not affiliated with Xiaohongshu)
AppPublisherURL=https://github.com/StellarStar255/xhs-research-agent
DefaultDirName={localappdata}\Programs\XHS Research Agent
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
OutputDir=..\dist
OutputBaseFilename=XHS-Research-Agent-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\XHS Research Agent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

; User data (login, chats, settings) lives in %USERPROFILE%\.xhs-research-agent and is
; kept on uninstall so reinstalling doesn't log the user out.
