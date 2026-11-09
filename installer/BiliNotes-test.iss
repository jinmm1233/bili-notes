; ============================================================================
; BiliNotes 安装流程自测脚本（不是给用户装的）
;
; 正式安装包用 BiliNotes.iss。这个脚本编译出的是「隔离版」：
;   - 独立 AppId，不碰正式安装的注册表项
;   - PrivilegesRequired=lowest，不用 UAC，普通会话就能跑
;   - 默认装到 %LOCALAPPDATA%\BiliNotesInstallTest
;   - CloseApplications=no，免得被正在运行的实例挡住
; 用途：改了 [Files]/[Icons]/[Run] 之后，在本机把「安装 → 释放文件 → 建快捷方式
; → 启动 → 卸载」整条流程跑一遍，确认没写错路径、没漏文件。
;
; 编译：ISCC.exe installer\BiliNotes-test.iss
; 安装：installer\dist-test\BiliNotes-installtest-0.2.4.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
; 卸载：安装目录下的 unins000.exe /VERYSILENT
; ============================================================================

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
#define MyAppVersion "0.2.4"
#define MyAppPublisher "BiliNotes"
#define MyAppExeName "BiliNotes.exe"

[Setup]
AppId={{TEST-ONLY-109BC6A1-2D75-47E6-8CB7-D32F0F5063D3}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\BiliNotesInstallTest
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist-test
OutputBaseFilename=BiliNotes-installtest-{#MyAppVersion}
SetupIconFile=..\app.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
; 测试包不检测“程序正在运行”，免得被用户开着的实例挡住
CloseApplications=no
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
// 坑：Edge --app 窗口的标题取自网页 <title>（"BiliNotes · B站视频知识库"），
// 不是 'BiliNotes'，FindWindowByWindowName 是精确匹配，只写 'BiliNotes' 永远拦不住。
// 所以两个标题都认，再兜一层进程检测——以后前端改标题也不会漏。
function BiliNotesIsRunning(): Boolean;
var
  Code: Integer;
begin
  Result := (FindWindowByWindowName('BiliNotes') <> 0) or
            (FindWindowByWindowName('BiliNotes · B站视频知识库') <> 0);
  if not Result then
  begin
    if Exec(ExpandConstant('{sys}\cmd.exe'),
            '/c tasklist /FI "IMAGENAME eq BiliNotes.exe" /NH | find /I "BiliNotes.exe" > NUL',
            '', SW_HIDE, ewWaitUntilTerminated, Code) then
      Result := (Code = 0);
  end;
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
