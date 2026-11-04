; the windows installer, built by buildexe.py when Inno Setup 6 is installed:
;   iscc /DAppVersion=1.2.0 installer.iss
;
; a small installer that always installs the newest release: it reads the latest release's
; update.json from github, downloads that version's app zip (checked against its sha256), clears
; out whatever version was installed before and puts the new one in its place. so an old copy of
; Scrapify-Setup.exe still installs the current version, and running it again updates or repairs.
;
; installs for the current user only (no admin), into %LOCALAPPDATA%\Programs\Scrapify, so the
; app can still update itself in place. start menu shortcut, optional desktop one, and an entry
; in Settings > Apps that uninstalls it

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define Repo "z6gg/Scrapify"
#define UninstallKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\{6F1C3E8A-5B2D-4E7F-9A31-2C8D4B7E0F15}_is1"

[Setup]
AppId={{6F1C3E8A-5B2D-4E7F-9A31-2C8D4B7E0F15}
AppName=Scrapify
AppVersion={#AppVersion}
AppPublisher=z6
AppPublisherURL=https://github.com/{#Repo}
AppSupportURL=https://github.com/{#Repo}/issues
AppUpdatesURL=https://github.com/{#Repo}/releases
VersionInfoVersion={#AppVersion}
VersionInfoProductName=Scrapify
VersionInfoDescription=Scrapify Setup
PrivilegesRequired=lowest
DefaultDirName={autopf}\Scrapify
DisableProgramGroupPage=yes
DisableDirPage=auto
WizardStyle=modern
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\Scrapify.exe
UninstallDisplayName=Scrapify
OutputDir=dist
; no version in the name, so releases/latest/download/Scrapify-Setup.exe is a link that never changes
OutputBaseFilename=Scrapify-Setup
Compression=lzma2
SolidCompression=yes
; no "close Scrapify first" prompt: setup closes the copies running from the install folder itself
CloseApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Messages]
ReadyLabel1=Setup will download the latest version of Scrapify from GitHub and install it.

[Tasks]
Name: "desktopicon"; Description: "Add a desktop shortcut"; Flags: unchecked

[Icons]
Name: "{autoprograms}\Scrapify"; Filename: "{app}\Scrapify.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Scrapify"; Filename: "{app}\Scrapify.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\Scrapify.exe"; Description: "Open Scrapify"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; the window and the background sync keep their files open. only copies running from this folder
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Get-Process Scrapify -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -and $_.Path.StartsWith('{app}\', 'OrdinalIgnoreCase') } | Stop-Process -Force; Start-Sleep -Seconds 1"""; Flags: runhidden; RunOnceId: "StopScrapify"

[UninstallDelete]
; the app files come from the download, not from the installer, so they're listed here
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\browsers"
Type: filesandordirs; Name: "{app}\.update"
Type: filesandordirs; Name: "{app}\.install"
Type: files; Name: "{app}\Scrapify.exe"
Type: files; Name: "{app}\LICENSE"
Type: dirifempty; Name: "{app}"

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  BackgroundEntry = 'Scrapify Background Sync';

var
  DownloadPage: TDownloadWizardPage;
  LatestVersion: String;
  BackgroundCommand: String;

function OnDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  Result := True;
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage('Installing Scrapify', 'Getting the latest version from GitHub...', @OnDownloadProgress);
end;

// the string value of "key" in update.json, which python writes as "key": "value"
function JsonString(const Json, Key: String): String;
var
  P: Integer;
  Rest: String;
begin
  Result := '';
  P := Pos('"' + Key + '"', Json);
  if P = 0 then
    Exit;
  Rest := Copy(Json, P + Length(Key) + 2, MaxInt);
  P := Pos('"', Rest);
  if P = 0 then
    Exit;
  Rest := Copy(Rest, P + 1, MaxInt);
  P := Pos('"', Rest);
  if P > 0 then
    Result := Copy(Rest, 1, P - 1);
end;

function AppDir: String;
begin
  Result := RemoveBackslashUnlessRoot(ExpandConstant('{app}'));
end;

// whether a scrapify start entry (start with windows, background sync) points at this install
function RunEntryIsOurs(const Name: String; var Command: String): Boolean;
begin
  Result := RegQueryStringValue(HKCU, RunKey, Name, Command) and
            (Pos(Lowercase(AppDir), Lowercase(Command)) > 0);
end;

function Run(const FileName, Params: String): Integer;
begin
  if not Exec(FileName, Params, '', SW_HIDE, ewWaitUntilTerminated, Result) then
    Result := -1;
end;

// every Scrapify.exe running from this folder: the window, the background sync
procedure StopRunningCopies;
begin
  Run(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoProfile -ExecutionPolicy Bypass -Command "Get-Process Scrapify -ErrorAction SilentlyContinue | ' +
      'Where-Object { $_.Path -and $_.Path.StartsWith(''' + AppDir + '\'', ''OrdinalIgnoreCase'') } | ' +
      'Stop-Process -Force"');
  Sleep(1000);  // let windows let go of the files
end;

function RemoveOldVersion: Boolean;
begin
  DelTree(AppDir + '\_internal', True, True, True);
  DelTree(AppDir + '\browsers', True, True, True);
  DelTree(AppDir + '\.update', True, True, True);
  DeleteFile(AppDir + '\Scrapify.exe');
  DeleteFile(AppDir + '\LICENSE');
  Result := not DirExists(AppDir + '\_internal') and not FileExists(AppDir + '\Scrapify.exe');
end;

// fetches the newest release, swaps it in for whatever was installed. '' when it worked
function DownloadAndInstall: String;
var
  Json: AnsiString;
  Sha, ZipUrl, ZipPath, Staging, Unpacked: String;
  Code: Integer;
begin
  Result := '';
  // 1. which version is the latest, and its zip's checksum
  DownloadPage.Clear;
  DownloadPage.Add('https://github.com/{#Repo}/releases/latest/download/update.json', 'latest.json', '');
  DownloadPage.Download;
  if not LoadStringFromFile(ExpandConstant('{tmp}\latest.json'), Json) then
    RaiseException('GitHub didn''t say which version is the latest.');
  LatestVersion := JsonString(Json, 'version');
  Sha := JsonString(Json, 'windows_zip_sha256');
  if LatestVersion = '' then
    RaiseException('GitHub didn''t say which version is the latest.');

  // 2. that version's app
  ZipUrl := Format('https://github.com/{#Repo}/releases/download/v%s/Scrapify-%s-windows.zip', [LatestVersion, LatestVersion]);
  DownloadPage.Clear;
  DownloadPage.Add(ZipUrl, 'scrapify.zip', Sha);
  DownloadPage.SetText('Downloading Scrapify ' + LatestVersion + '...', '');
  DownloadPage.Download;
  ZipPath := ExpandConstant('{tmp}\scrapify.zip');

  // 3. unpacked next to where it goes, so moving it in is instant
  DownloadPage.SetText('Unpacking Scrapify ' + LatestVersion + '...', 'This takes a few seconds.');
  DownloadPage.ProgressBar.Style := npbstMarquee;
  Staging := AppDir + '\.install';
  DelTree(Staging, True, True, True);
  if not ForceDirectories(Staging) then
  begin
    Result := 'Couldn''t create ' + AppDir + '.';
    Exit;
  end;
  Code := Run(ExpandConstant('{sys}\tar.exe'), '-xf "' + ZipPath + '" -C "' + Staging + '"');
  if Code <> 0 then
    // windows before 10 1803 has no tar
    Code := Run(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
                '-NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath ''' + ZipPath +
                ''' -DestinationPath ''' + Staging + ''' -Force"');
  Unpacked := Staging + '\Scrapify';
  if (Code <> 0) or not FileExists(Unpacked + '\Scrapify.exe') then
  begin
    DelTree(Staging, True, True, True);
    Result := 'Couldn''t unpack the download. Try running Setup again.';
    Exit;
  end;

  // 4. out with the old version, in with the new
  DownloadPage.SetText('Installing Scrapify ' + LatestVersion + '...', '');
  if not RunEntryIsOurs(BackgroundEntry, BackgroundCommand) then
    BackgroundCommand := '';
  StopRunningCopies;
  if not RemoveOldVersion then
  begin
    DelTree(Staging, True, True, True);
    Result := 'Couldn''t remove the old version, a file in ' + AppDir + ' is still in use. ' +
              'Close Scrapify (also from the tray) and run Setup again.';
    Exit;
  end;
  if not (RenameFile(Unpacked + '\_internal', AppDir + '\_internal') and
          RenameFile(Unpacked + '\browsers', AppDir + '\browsers') and
          RenameFile(Unpacked + '\Scrapify.exe', AppDir + '\Scrapify.exe')) then
  begin
    Result := 'Couldn''t move the new files into ' + AppDir + '. Run Setup again.';
    Exit;
  end;
  RenameFile(Unpacked + '\LICENSE', AppDir + '\LICENSE');
  DelTree(Staging, True, True, True);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  try
    if not WizardSilent then
      DownloadPage.Show;
    try
      Result := DownloadAndInstall;
    finally
      if not WizardSilent then
        DownloadPage.Hide;
    end;
  except
    if DownloadPage.AbortedByUser then
      Result := 'The download was cancelled, nothing was installed.'
    else
      Result := 'Couldn''t download Scrapify: ' + GetExceptionMessage + #13#10#13#10 +
                'Check your internet connection and run Setup again.';
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    // settings > apps shows the version that was downloaded, not the installer's own
    RegWriteStringValue(HKCU, '{#UninstallKey}', 'DisplayVersion', LatestVersion);
    RegWriteStringValue(HKCU, '{#UninstallKey}', 'DisplayName', 'Scrapify');
    // the background sync was stopped to replace its files, start it again
    if BackgroundCommand <> '' then
      ShellExec('', ExpandConstant('{cmd}'), '/c start "" ' + BackgroundCommand, ExpandConstant('{localappdata}\Scrapify'),
                SW_HIDE, ewNoWait, ErrorCode);
  end;
end;

// start with windows and the background sync are entries scrapify itself adds. only the ones
// pointing at this install go, a portable copy elsewhere keeps its own
procedure RemoveRunEntry(const Name: String);
var
  Command: String;
begin
  if RunEntryIsOurs(Name, Command) then
    RegDeleteValue(HKCU, RunKey, Name);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    RemoveRunEntry('Scrapify');
    RemoveRunEntry(BackgroundEntry);
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
