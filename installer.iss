; the windows installer, built by buildexe.py when Inno Setup 6 is installed:
;   iscc /DAppVersion=1.2.0 installer.iss
; installs for the current user only (no admin), into %LOCALAPPDATA%\Programs\Scrapify, so the
; app can still update itself in place. start menu shortcut, optional desktop one, and an entry
; in Settings > Apps that uninstalls it

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F1C3E8A-5B2D-4E7F-9A31-2C8D4B7E0F15}
AppName=Scrapify
AppVersion={#AppVersion}
AppVerName=Scrapify {#AppVersion}
AppPublisher=z6
AppPublisherURL=https://github.com/z6gg/Scrapify
AppSupportURL=https://github.com/z6gg/Scrapify/issues
AppUpdatesURL=https://github.com/z6gg/Scrapify/releases
VersionInfoVersion={#AppVersion}
VersionInfoProductName=Scrapify
PrivilegesRequired=lowest
DefaultDirName={autopf}\Scrapify
DisableProgramGroupPage=yes
DisableDirPage=auto
DisableReadyPage=yes
WizardStyle=modern
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\Scrapify.exe
UninstallDisplayName=Scrapify
OutputDir=dist
OutputBaseFilename=Scrapify-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
; a running copy holds its files open: ask to close it (the window's single instance mutex)
AppMutex=ScrapifyGUISingleInstance
CloseApplications=yes
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Add a desktop shortcut"; Flags: unchecked

[Files]
Source: "dist\Scrapify\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Scrapify"; Filename: "{app}\Scrapify.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Scrapify"; Filename: "{app}\Scrapify.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\Scrapify.exe"; Description: "Open Scrapify"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; the background sync runs from the same exe and keeps its files open
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM Scrapify.exe"; Flags: runhidden; RunOnceId: "StopScrapify"

[UninstallDelete]
; the app updates itself, so what's in the folder now isn't what the installer put there
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\browsers"
Type: filesandordirs; Name: "{app}\.update"
Type: files; Name: "{app}\Scrapify.exe"
Type: files; Name: "{app}\LICENSE"
Type: dirifempty; Name: "{app}"

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';

// start with windows and the background sync are entries scrapify itself adds. only the ones
// pointing at this install go, a portable copy elsewhere keeps its own
procedure RemoveRunEntry(Name: String);
var
  Command: String;
begin
  if RegQueryStringValue(HKCU, RunKey, Name, Command) then
    if Pos(Lowercase(ExpandConstant('{app}')), Lowercase(Command)) > 0 then
      RegDeleteValue(HKCU, RunKey, Name);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    RemoveRunEntry('Scrapify');
    RemoveRunEntry('Scrapify Background Sync');
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\Scrapify');
    if DirExists(DataDir) and not UninstallSilent then
      if MsgBox('Also delete your Scrapify settings, logins and sync log?' + #13#10#13#10 +
                'Keep them if you might install Scrapify again.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
