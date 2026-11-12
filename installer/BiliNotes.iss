; ============================================================================
; BiliNotes 安装包脚本（Inno Setup 6）
;
; 编译：ISCC.exe installer\BiliNotes.iss
; 前置：先跑 python -m PyInstaller build.spec --noconfirm（产物 dist\BiliNotes\）
; 产物：installer\dist\BiliNotes-setup-<版本>.exe
;
; 设计决定：
;   - 装到 Program Files，数据仍然在 %APPDATA%\BiliNotes（笔记在用户选的目录），
;     升级覆盖安装、卸载后重装，笔记和配置都不丢。
;   - VC++ 运行库自动检测：没有就静默装微软官方 vc_redist（ctranslate2 缺它起不来）。
;   - 没有代码签名时 SmartScreen 会提示"未知发布者"，见 docs/发布清单.md。
; ============================================================================

#define MyAppName "BiliNotes"
#define MyAppVersion "0.2.6"
#define MyAppPublisher "BiliNotes"
#define MyAppExeName "BiliNotes.exe"

[Setup]
AppId={{109BC6A1-2D75-47E6-8CB7-D32F0F5063D3}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=BiliNotes-setup-{#MyAppVersion}
SetupIconFile=..\app.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
; 卸载时不碰 %APPDATA%\BiliNotes（用户数据目录），默认就是不碰
UninstallDisplayName={#MyAppName}

[Languages]
Name: "chinese"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\BiliNotes\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion createallsubdirs
; VC++ 运行库：ctranslate2（语音转写）需要，缺了启动也不报错、转写时才炸
; 一份放 {tmp} 供安装时静默执行，一份留在安装目录——环境自检的提示语会告诉
; 用户「双击安装目录里的 redist\vc_redist.x64.exe」，这里必须真有这个文件。
Source: "redist\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "redist\vc_redist.x64.exe"; DestDir: "{app}\redist"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName}（控制台模式）"; Filename: "{app}\BiliNotes-console.exe"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{tmp}\vc_redist.x64.exe"; \
  Parameters: "/install /quiet /norestart"; \
  StatusMsg: "正在安装 VC++ 运行库（语音转写需要，约 25MB）…"; \
  Check: NeedVCRuntime; Flags: waituntilterminated
Filename: "{app}\{#MyAppExeName}"; \
  Description: "{cm:LaunchProgram,{#MyAppName}}"; \
  Flags: nowait postinstall skipifsilent

[Code]
// 缺 VC++ 运行库才装（msvcp140 / vcruntime140_1 任缺一个都算缺）
function NeedVCRuntime(): Boolean;
begin
  Result := (not FileExists(ExpandConstant('{sys}\msvcp140.dll'))) or
            (not FileExists(ExpandConstant('{sys}\vcruntime140_1.dll')));
end;

// 程序在跑就不能装/卸：exe 被占用会装一半失败，用户只看到一个错误框。
//
// 坑（实测踩出来的）：Inno 6.7 的 FindWindowByWindowName 对含中文的窗口标题
// 会返回乱七八糟的非 0 值——查一个根本不存在的 "BiliNotes · B站视频知识库"
// 居然返回 202050。原来的写法（窗口标题 + tasklist 双保险）里，
// 窗口标题这一路永远为真，于是**所有用户装包都会误弹「好像正在运行」**，
// 静默安装模式下这个框会一直等，表现为「安装程序卡住不动」。
//
// 现在只信 tasklist（查 BiliNotes.exe 进程）：实测准确，找不到时退出码 1。
// Edge --app 那个窗口是 BiliNotes.exe 启的子进程，BiliNotes.exe 本身也在跑，
// 所以查 exe 进程一样能拦住。
function BiliNotesIsRunning(): Boolean;
var
  Code: Integer;
begin
  Result := False;
  if Exec(ExpandConstant('{sys}\cmd.exe'),
          '/c tasklist /FI "IMAGENAME eq BiliNotes.exe" /NH | find /I "BiliNotes.exe" > NUL',
          '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Result := (Code = 0);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if BiliNotesIsRunning() then begin
    if MsgBox('BiliNotes 好像正在运行，继续安装可能失败。' #13#10 +
              '请先关掉它的窗口，然后点「重试」。' #13#10 #13#10 +
              '仍要继续吗？', mbError, MB_YESNO) = IDNO then
      Result := False;
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  if BiliNotesIsRunning() then begin
    MsgBox('BiliNotes 正在运行，请先关掉它的窗口再卸载。', mbError, MB_OK);
    Result := False;
  end;
end;

// 卸载完成时告诉用户数据还在哪里、怎么删干净
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then begin
    MsgBox('卸载完成。' #13#10 #13#10 +
           '你的笔记和配置都保留在原处，没有删除：' #13#10 +
           '· 配置与卡片：%APPDATA%\BiliNotes' #13#10 +
           '· 笔记文件：生成时你选择的输出目录' #13#10 #13#10 +
           '确定不再用了，可以手动删除上面这些文件夹。', mbInformation, MB_OK);
  end;
end;
