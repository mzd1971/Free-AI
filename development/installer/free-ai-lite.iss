#ifndef SourceRoot
  #error SourceRoot is required
#endif
#ifndef StageRoot
  #error StageRoot is required
#endif
#ifndef ReleaseDir
  #error ReleaseDir is required
#endif
#define AppVersion "2.3.1"
[Setup]
AppId={{3EAACF9D-4709-48F0-AF84-0275969D1600}
AppName=Free AI
AppVersion={#AppVersion}
AppPublisher=Free AI
DefaultDirName={code:GetDefaultDirectory}
DefaultGroupName=Free AI
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
UsePreviousAppDir=no
OutputDir={#ReleaseDir}
OutputBaseFilename=FreeAI-Setup-{#AppVersion}
SetupIconFile={#SourceRoot}\assets\free-ai.ico
UninstallDisplayIcon={app}\Free AI.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
WizardResizable=yes
WizardSizePercent=110
CloseApplications=yes
RestartApplications=no
InfoBeforeFile={#SourceRoot}\安装说明.txt
[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
[Tasks]
Name: "desktopicon"; Description: "Free AI — 日常聊天与使用能力（推荐）"; GroupDescription: "桌面快捷方式："; Flags: checkedonce
Name: "trainerdesktopicon"; Description: "Free AI 训练端 — 准备数据、训练与查看进步（可选）"; GroupDescription: "桌面快捷方式："; Flags: unchecked
[Files]
Source: "{#StageRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\Free AI"; Filename: "{app}\Free AI.exe"; WorkingDir: "{app}"
Name: "{group}\Free AI 训练端"; Filename: "{app}\Free AI Trainer.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Free AI"; Filename: "{app}\Free AI.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{autodesktop}\Free AI 训练端"; Filename: "{app}\Free AI Trainer.exe"; WorkingDir: "{app}"; Tasks: trainerdesktopicon
[Run]
Filename: "{app}\Free AI.exe"; Description: "打开 Free AI"; Flags: nowait postinstall skipifsilent
[Code]
var
  ModelPage: TInputDirWizardPage;
  InsideCheck: TNewCheckBox;
  ModelInitialApp: String;
  OriginalValue: String;
  OriginalExists: Boolean;
  ModelChanged: Boolean;
  LoadingModel: Boolean;
  CommandModel: String;
  HasCommandModel: Boolean;

function GetDriveTypeW(RootPathName: String): Cardinal;
  external 'GetDriveTypeW@kernel32.dll stdcall';

function LocalDataDirectory(Value: String): Boolean;
var Drive, Tail, Part, Stem, Parent: String; Index, Separator: Integer;
begin
  Result := False;
  StringChangeEx(Value, '/', '\', True);
  if (Length(Value) < 4) or (Copy(Value, 2, 2) <> ':\') then Exit;
  Drive := Uppercase(Copy(Value, 1, 1));
  if (Drive < 'A') or (Drive > 'Z') or (Drive = 'C') then Exit;
  for Index := 1 to Length(Value) do begin
    if (Ord(Value[Index]) < 32) or (Ord(Value[Index]) = 127) then Exit;
    if Pos(Value[Index], '<>"|?*') > 0 then Exit;
    if (Value[Index] = ':') and (Index <> 2) then Exit;
  end;
  Tail := Copy(Value, 4, Length(Value));
  while Tail <> '' do begin
    Separator := Pos('\', Tail);
    if Separator = 0 then begin Part := Tail; Tail := ''; end
      else begin Part := Copy(Tail, 1, Separator - 1); Delete(Tail, 1, Separator); end;
    if Part <> '' then begin
      if (Part = '..') or (Copy(Part, Length(Part), 1) = '.') or (Copy(Part, Length(Part), 1) = ' ') then Exit;
      Separator := Pos('.', Part);
      if Separator > 0 then Stem := Uppercase(Copy(Part, 1, Separator - 1)) else Stem := Uppercase(Part);
      if (Stem = 'CON') or (Stem = 'PRN') or (Stem = 'AUX') or (Stem = 'NUL') then Exit;
      for Index := 1 to 9 do if (Stem = 'COM' + IntToStr(Index)) or (Stem = 'LPT' + IntToStr(Index)) then Exit;
    end;
  end;
  if not DirExists(Copy(Value, 1, 3)) then Exit;
  if GetDriveTypeW(Copy(Value, 1, 3)) = 4 then Exit;
  Parent := Value;
  while Length(Parent) > 3 do begin
    if FileExists(Parent) then Exit;
    Tail := ExtractFileDir(Parent);
    if Tail = Parent then Break;
    Parent := Tail;
  end;
  Result := True;
end;

function GetDefaultDirectory(Param: String): String;
var Previous: String; Letter: Integer; Drive: String;
begin
  if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{3EAACF9D-4709-48F0-AF84-0275969D1600}_is1', 'Inno Setup: App Path', Previous) then
    if LocalDataDirectory(Previous) and DirExists(Previous) then begin Result := Previous; Exit; end;
  if DirExists('D:\') then begin Result := 'D:\FreeAI'; Exit; end;
  for Letter := Ord('E') to Ord('Z') do begin
    Drive := Chr(Letter) + ':\';
    if DirExists(Drive) then begin Result := Drive + 'FreeAI'; Exit; end;
  end;
  Result := 'D:\FreeAI';
end;

function DirectoryHasContent(Value: String): Boolean;
var Item: TFindRec;
begin
  Result := False;
  if FindFirst(AddBackslash(Value) + '*', Item) then begin
    try
      repeat
        if (Item.Name <> '.') and (Item.Name <> '..') then begin Result := True; Break; end;
      until not FindNext(Item);
    finally FindClose(Item); end;
  end;
end;

procedure ModelEdited(Sender: TObject);
begin
  if not LoadingModel then ModelChanged := True;
end;

procedure InsideClicked(Sender: TObject);
begin
  if not LoadingModel then ModelChanged := True;
  ModelPage.Edits[0].Enabled := not InsideCheck.Checked;
  ModelPage.Buttons[0].Enabled := not InsideCheck.Checked;
  if InsideCheck.Checked then ModelPage.Values[0] := AddBackslash(WizardDirValue) + 'models';
end;

procedure RefreshModelSelection();
var Lines: TArrayOfString; ConfigFile, Value, Legacy: String;
begin
  if ModelInitialApp = WizardDirValue then Exit;
  ModelInitialApp := WizardDirValue;
  ConfigFile := AddBackslash(ModelInitialApp) + 'model-location.txt';
  OriginalExists := FileExists(ConfigFile);
  OriginalValue := '';
  if OriginalExists and LoadStringsFromFile(ConfigFile, Lines) and (GetArrayLength(Lines) = 1) then OriginalValue := Trim(Lines[0]);
  Value := OriginalValue;
  if not OriginalExists then begin
    Legacy := AddBackslash(ModelInitialApp) + 'portable_data\capabilities';
    if DirectoryHasContent(Legacy) then Value := Legacy else Value := 'models';
  end;
  if HasCommandModel then Value := CommandModel;
  LoadingModel := True;
  InsideCheck.Checked := CompareText(Value, 'models') = 0;
  if InsideCheck.Checked then ModelPage.Values[0] := AddBackslash(ModelInitialApp) + 'models'
    else if (Value <> '') and (ExtractFileDrive(Value) = '') and (Copy(Value, 1, 1) <> '\') then
      ModelPage.Values[0] := AddBackslash(ModelInitialApp) + Value
    else ModelPage.Values[0] := Value;
  InsideClicked(InsideCheck);
  LoadingModel := False;
  ModelChanged := False;
end;

procedure InitializeWizard();
var Index: Integer;
begin
  HasCommandModel := False;
  for Index := 1 to ParamCount do
    if CompareText(Copy(ParamStr(Index), 1, 10), '/MODELDIR=') = 0 then HasCommandModel := True;
  CommandModel := Trim(ExpandConstant('{param:MODELDIR|}'));
  StringChangeEx(CommandModel, '/', '\', True);
  ModelPage := CreateInputDirPage(wpSelectDir, '模型保存在哪里',
    '模型按需下载；安装包不会附带模型。',
    '默认放在 FreeAI 里面，方便整体迁移。升级会保留原位置。改选位置不会自动搬动或删除旧模型。', False, 'FreeAI Models');
  ModelPage.Add('模型文件夹（非 C 盘）：');
  ModelPage.Edits[0].Top := ModelPage.Edits[0].Top + ScaleY(40);
  ModelPage.Buttons[0].Top := ModelPage.Buttons[0].Top + ScaleY(40);
  ModelPage.Edits[0].OnChange := @ModelEdited;
  InsideCheck := TNewCheckBox.Create(ModelPage);
  InsideCheck.Parent := ModelPage.Surface;
  InsideCheck.Left := 0;
  InsideCheck.Top := ModelPage.Edits[0].Top - ScaleY(30);
  InsideCheck.Width := ModelPage.SurfaceWidth;
  InsideCheck.Caption := '模型放在 FreeAI 里面（推荐）';
  InsideCheck.OnClick := @InsideClicked;
  RefreshModelSelection();
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = ModelPage.ID then RefreshModelSelection();
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpSelectDir then begin
    Result := LocalDataDirectory(WizardDirValue);
    if not Result then MsgBox('请选择非 C 盘的本地应用文件夹，例如 D:\FreeAI。', mbError, MB_OK);
  end;
  if CurPageID = ModelPage.ID then begin
    Result := LocalDataDirectory(ModelPage.Values[0]);
    if not Result then MsgBox('请选择非 C 盘的完整本地模型目录，或勾选推荐位置。', mbError, MB_OK);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  RefreshModelSelection();
  if not LocalDataDirectory(WizardDirValue) then Result := '安装目录必须为非 C 盘的本地文件夹。'
  else if not LocalDataDirectory(ModelPage.Values[0]) then Result := '模型目录无效，请使用 /MODELDIR=models 或非 C 盘的完整本地目录。';
  if Result = '' then Log('Free AI model directory selected: ' + ModelPage.Values[0]);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var Lines: TArrayOfString; Value: String;
begin
  if CurStep <> ssPostInstall then Exit;
  if OriginalExists and not ModelChanged and not HasCommandModel then begin
    Log('Free AI model-location.txt preserved: ' + OriginalValue);
    Exit;
  end;
  if InsideCheck.Checked then Value := 'models'
    else if HasCommandModel and not ModelChanged then Value := CommandModel
    else Value := ModelPage.Values[0];
  if not ForceDirectories(ModelPage.Values[0]) then RaiseException('无法创建模型文件夹，请检查所选目录。');
  SetArrayLength(Lines, 1); Lines[0] := Value;
  if not SaveStringsToUTF8File(ExpandConstant('{app}\model-location.txt'), Lines, False) then RaiseException('无法保存模型位置。');
  Log('Free AI model-location.txt written as UTF-8 BOM: ' + Value);
end;

// No recursive install/uninstall deletion: personal data, models and location survive.
