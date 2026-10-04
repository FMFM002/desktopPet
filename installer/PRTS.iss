; ══════════════════════════════════════════════════════════════
;  铃兰桌宠（PRTS Desktop Pet）—— Inno Setup 安装脚本（发布形态 C）
;
;  ★ 用户定的顺序是 A（便携 zip）→ B（exe）→ C（安装程序），这是 C。
;
;  用户原话："是否下载这些包，在安装程序里面询问，后面一起装了。"
;    → 见 [Tasks] 里的 models 复选框 + [Run] 里交给 model_manager.py install
;
;  ★ 前置：装 Inno Setup 6（https://jrsoftware.org/isdl.php），然后
;      "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\PRTS.iss
;    本机开发环境**没有装 Inno Setup**（实测），所以这个脚本**没有被编译验证过** ——
;    这点如实记录，第一次编译若报错请把输出给我。
;
;  ★ 打包对象用 **B（PyInstaller onedir）的产物**：dist\PRTS-DesktopPet-1.9.4\
;    因为 C 面向的是"什么都不想装"的用户，B 已经把 Python 运行时带上了。
; ══════════════════════════════════════════════════════════════

#define AppName "铃兰桌宠"
#define AppNameEn "PRTS Desktop Pet"
#define AppVer "1.9.4"
#define AppPublisher "PRTS"
#define AppExeName "PRTS-DesktopPet-1.9.4.exe"
; ★ 源目录：PyInstaller 的 onedir 产物（先跑 PRTS.spec 生成）
#define SrcDir "..\dist\PRTS-DesktopPet-1.9.4"

[Setup]
AppId={{8E2B1C44-9F3A-4D77-9C21-PRTS19400000}
AppName={#AppName}
AppVersion={#AppVer}
AppVerName={#AppName} {#AppVer}
AppPublisher={#AppPublisher}
; ★ 装到用户目录，避开"要管理员权限"——桌宠是个人软件，不需要全机器安装
DefaultDirName={autopf}\{#AppNameEn}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputBaseFilename={#AppNameEn}-{#AppVer}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; ★ 不要求管理员：装到用户目录 + 不需要写注册表敏感位置
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinese"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："
; ★★ 用户要求的那一项：在安装时**询问**要不要一起把模型装好
Name: "models"; Description: "同时下载模型（语音合成/识别/OCR，约 2.4 GB，需要联网）"; GroupDescription: "可选组件："; Flags: unchecked

[Files]
; 整个 onedir 产物（PRTS.exe + _internal）
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; ★ 便携版的启动器与必读也一起带上（万一用户想直接看命令行输出）
Source: "{#SrcDir}\启动.bat"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\首次使用必读"; Filename: "{app}\首次使用必读.txt"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; ★ 装完立刻跑"首次配置引导"（用户第一次双击就能填 LLM Key）
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent
; ★★ 只有在用户勾了 models 时才执行。
;    用 B 产物里的 Python 运行时跑不了我们的 .py 脚本（脚本在源码树里），
;    所以这里**调用户机器上的 python**；没有 python 就跳过并提示
;    （B 形态本来就不需要 python，所以这条只是"能下就下"）。
Filename: "python"; Parameters: """{app}\tools\model_manager.py"" install --all --yes"; \
    WorkingDir: "{app}"; StatusMsg: "正在获取模型（可能较慢）…"; \
    Flags: runhidden waituntilterminated skipifdoesntexist; Tasks: models
