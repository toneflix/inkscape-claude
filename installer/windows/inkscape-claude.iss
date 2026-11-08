; Inkscape Claude installer for Windows. Built by scripts/build_installer.py, which passes
; AppVersion, Stage (the staged files), OutputDir and OutputName.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6B1E5F3C-2D7A-4C61-9E1B-7A3C0F2D8E45}
AppName=Inkscape Claude
AppVersion={#AppVersion}
AppPublisher=Inkscape Claude
AppPublisherURL=https://github.com/toneflix/inkscape-claude
DefaultDirName={autopf}\Inkscape Claude
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ChangesEnvironment=yes
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=inkscape-claude.ico
UninstallDisplayIcon={app}\inkscape-claude.ico
UninstallDisplayName=Inkscape Claude
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[InstallDelete]
Type: filesandordirs; Name: "{userappdata}\inkscape\extensions\claude"

[Files]
Source: "{#Stage}\extension\claude\*"; DestDir: "{userappdata}\inkscape\extensions\claude"; Flags: ignoreversion recursesubdirs
Source: "{#Stage}\bin\inkscape-mcp.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "inkscape-claude.ico"; DestDir: "{app}"; Flags: ignoreversion

[UninstallDelete]
Type: filesandordirs; Name: "{userappdata}\inkscape\extensions\claude"

[Messages]
FinishedLabel=Restart Inkscape, then find Ask Claude and Open Claude Chat under Extensions > Claude.%n%nThe MCP server is inkscape-mcp.exe in [name]'s folder and on your PATH. In Claude Code run:%n%n    claude mcp add inkscape -- inkscape-mcp

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Check: NeedsAddPath(ExpandConstant('{app}'))

[Code]
function PathContains(Paths, Folder: string): Boolean;
begin
  Result := Pos(';' + Uppercase(Folder) + ';', ';' + Uppercase(Paths) + ';') > 0;
end;

function NeedsAddPath(Folder: string): Boolean;
var
  Paths: string;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Paths) then
    Paths := '';
  Result := not PathContains(Paths, Folder);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Paths, Folder: string;
  Start: Integer;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Paths) then
    exit;

  Folder := ExpandConstant('{app}');
  Paths := ';' + Paths + ';';
  Start := Pos(';' + Uppercase(Folder) + ';', Uppercase(Paths));
  if Start = 0 then
    exit;

  Delete(Paths, Start, Length(Folder) + 1);
  Paths := Copy(Paths, 2, Length(Paths) - 2);
  RegWriteExpandStringValue(HKCU, 'Environment', 'Path', Paths);
end;
