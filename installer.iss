; Instalador do Mutinho (Inno Setup 6+). Compile com:  python make_installer.py
; Pré-requisitos na pasta do projeto:
;   siteMutinho\dist\Mutinho.exe     (gerado pelo PyInstaller)
;   vbcable\                          (conteúdo extraído do pacote do VB-CABLE, de vb-audio.com/Cable)

#ifndef MyAppVersion
  #define MyAppVersion "1.6.3"
#endif
#define MyAppName "Mutinho"
#define MyAppExe "Mutinho.exe"

[Setup]
AppId={{B7C3F2A1-5D4E-4F7A-9C1B-3E8A6D2F4B90}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
VersionInfoVersion={#MyAppVersion}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoDescription=Instalador do {#MyAppName} {#MyAppVersion}
DisableWelcomePage=no
AppPublisher=Nicolas
; Instala na pasta do usuário: assim o app consegue se atualizar sozinho (não precisa de administrador)
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=siteMutinho\dist
OutputBaseFilename=MutinhoSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#MyAppExe}

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "vbcable"; Description: "Instalar o microfone virtual (VB-CABLE), necessário para a redução de ruído funcionar no Meet, Discord e Zoom"; GroupDescription: "Redução de ruído:"
Name: "autostart"; Description: "Iniciar o Mutinho junto com o Windows"; GroupDescription: "Opções:"; Flags: unchecked
Name: "desktopicon"; Description: "Criar atalho na área de trabalho"; GroupDescription: "Opções:"

[Files]
Source: "siteMutinho\dist\Mutinho.exe"; DestDir: "{app}"; Flags: ignoreversion
; Pacote do VB-CABLE: vai para uma pasta temporária e é apagado ao fim da instalação
Source: "vbcable\*"; DestDir: "{tmp}\vbcable"; Flags: recursesubdirs createallsubdirs deleteafterinstall; Tasks: vbcable

[Icons]
Name: "{autoprograms}\Mutinho"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\Mutinho"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon
Name: "{userstartup}\Mutinho"; Filename: "{app}\{#MyAppExe}"; Tasks: autostart

[Run]
; "-i -h": instala sem a tela do instalador do VB-CABLE. O Windows ainda pede a confirmação do driver.
Filename: "{tmp}\vbcable\VBCABLE_Setup_x64.exe"; Parameters: "-i -h"; StatusMsg: "Instalando o microfone virtual (aceite as janelas de permissão do Windows)..."; Flags: shellexec runhidden waituntilterminated; Verb: runas; Tasks: vbcable
Filename: "{app}\{#MyAppExe}"; Description: "Abrir o Mutinho"; Flags: nowait postinstall skipifsilent
Filename: "https://vb-cable.com"; Description: "Conhecer e apoiar o VB-CABLE (donationware)"; Flags: shellexec postinstall skipifsilent unchecked

[Code]
var
  CreditPage: TOutputMsgWizardPage;

procedure InitializeWizard;
begin
  { Crédito exigido pela licença do VB-CABLE: origem, natureza donationware e como apoiar }
  CreditPage := CreateOutputMsgPage(wpSelectTasks,
    'Microfone virtual (VB-CABLE)',
    'Sobre o componente que será instalado',
    'A redução de ruído do Mutinho envia o áudio limpo para um microfone virtual. ' +
    'Para isso, este instalador inclui o VB-CABLE, criado pela VB-Audio Software (vb-cable.com).' + #13#10 + #13#10 +
    'VB-CABLE é donationware: todas as contribuições são bem-vindas. ' +
    'Se ele for útil para você, considere apoiar o projeto em vb-cable.com.' + #13#10 + #13#10 +
    'Durante a instalação, o Windows vai pedir permissão de administrador e a confirmação do driver. ' +
    'Depois será preciso reiniciar o computador.');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = CreditPage.ID) and (not WizardIsTaskSelected('vbcable'));
end;

function NeedRestart(): Boolean;
begin
  { O driver de áudio só aparece para os aplicativos depois de reiniciar }
  Result := WizardIsTaskSelected('vbcable');
end;
