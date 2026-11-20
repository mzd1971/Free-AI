[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Compiler = (Join-Path $PSScriptRoot 'tools\InnoSetup7.1.1\ISCC.exe'),
    [string]$ReleaseDirectory = 'D:\FreeAI-Releases',
    [switch]$PrepareOnly
)
$ErrorActionPreference = 'Stop'
$taskVersion = '2.8.0'
$taskDevelopment = $PSScriptRoot
$taskRuntime = Join-Path $SourceRoot 'runtime\python'
$taskStage = Join-Path $taskDevelopment ('stage\lite-' + $taskVersion + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
$taskStageRuntime = Join-Path $taskStage 'runtime\python'
$taskReports = Join-Path $taskDevelopment 'reports'
$taskPrivatePattern = '(?i)(^|[-_.])(api[-_]?connection|api[-_]?settings|settings|secrets?|credentials?|api[-_]?keys?|tokens?)([-_.]|$)|^\.env($|\.)'
$taskRequired = @('free_ai_bootstrap.py','client_app.py','client_settings.py','coding_tools.py','ui_preferences.py','hf_models.py','hf_model_panel.py','hf_datasets.py','hf_dataset_catalog.py','hf_dataset_panel.py','trainer_app.py','ui_kit.py','compute_guard.py','model_catalog.py','app_skills.py','api_panel.py','api_settings.py','remote_api.py','project_agent.py','project_chat.py','agent_workspace.py','agent_task_state.py','agent_review.py','local_project_model.py','agent_memory.py','memory_learning.py','memory_panel.py','paths.py','assets\free-ai.ico')
$taskRequired += @('acp_transport.py','harness_agent.py','harness_settings.py','harness_panel.py')
$taskRequired += @('project_snapshot.py','project_verification.py','project_checks.py')
foreach ($taskName in $taskRequired) {
    if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot $taskName) -PathType Leaf)) { throw "Missing application source: $taskName" }
}
New-Item -ItemType Directory -Path $taskStageRuntime,$taskReports -Force | Out-Null
$taskRootRuntimeFiles = @('python.exe','pythonw.exe','python3.dll','python311.dll','vcruntime140.dll','vcruntime140_1.dll','msvcp140.dll','LICENSE.txt')
foreach ($taskName in $taskRootRuntimeFiles) {
    Copy-Item -LiteralPath (Join-Path $taskRuntime $taskName) -Destination (Join-Path $taskStageRuntime $taskName)
}
foreach ($taskSection in @('Lib','DLLs','tcl')) {
    foreach ($taskFile in Get-ChildItem -LiteralPath (Join-Path $taskRuntime $taskSection) -File -Recurse -Force) {
        $taskRelative = $taskFile.FullName.Substring($taskRuntime.TrimEnd('\').Length + 1)
        if ($taskRelative -match '(^|[\\/])(site-packages|__pycache__|test|tests|idlelib|demos|turtledemo|tix8\.4\.3)([\\/]|$)') { continue }
        if ($taskFile.Extension -in @('.pyc','.pyo') -or $taskFile.Name -like '_test*' -or ($taskSection -eq 'tcl' -and $taskFile.Extension -eq '.lib')) { continue }
        if (($taskFile.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Runtime links are not packaged.' }
        if ($taskFile.Extension -notin @('.py','.pyi') -and $taskFile.Name -match $taskPrivatePattern) { throw "Private runtime configuration: $taskRelative" }
        $taskDestination = Join-Path $taskStageRuntime $taskRelative
        New-Item -ItemType Directory -Path (Split-Path -Parent $taskDestination) -Force | Out-Null
        Copy-Item -LiteralPath $taskFile.FullName -Destination $taskDestination
    }
}
# Root code and reviewed static files only: no configuration, models or development tree.
foreach ($taskFile in Get-ChildItem -LiteralPath $SourceRoot -File -Filter '*.py') {
    Copy-Item -LiteralPath $taskFile.FullName -Destination (Join-Path $taskStage $taskFile.Name)
}
$taskStatic = @('capability_catalog.json','使用说明.md','assets\free-ai.ico','assets\free-ai-ui.png')
$taskStarter = @('sft_general_test.jsonl','sft_test.jsonl','sft_train.jsonl','sft_validation.jsonl','train.txt','validation.txt')
foreach ($taskName in $taskStarter) { $taskStatic += 'starter_data\teaching\' + $taskName }
foreach ($taskRelative in $taskStatic) {
    $taskSource = Join-Path $SourceRoot $taskRelative
    if ($taskRelative -like 'starter_data\*' -and (Get-Item -LiteralPath $taskSource).Length -gt 256KB) { throw 'Starter example exceeds reviewed limit.' }
    $taskDestination = Join-Path $taskStage $taskRelative
    New-Item -ItemType Directory -Path (Split-Path -Parent $taskDestination) -Force | Out-Null
    Copy-Item -LiteralPath $taskSource -Destination $taskDestination
}
Copy-Item -LiteralPath (Join-Path $SourceRoot '安装说明.txt') -Destination (Join-Path $taskStage '安装说明.txt')
$taskCsc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
foreach ($taskEntry in @('Free AI.exe','Free AI Trainer.exe')) {
    $taskArguments = @('/nologo','/target:winexe','/platform:x64','/optimize+','/reference:System.Windows.Forms.dll',('/win32icon:' + (Join-Path $SourceRoot 'assets\free-ai.ico')),('/out:' + (Join-Path $taskStage $taskEntry)))
    if ($taskEntry -like '*Trainer*') { $taskArguments += '/define:TRAINER' }
    & $taskCsc @taskArguments (Join-Path $taskDevelopment 'launcher\FreeAI.cs')
    if ($LASTEXITCODE -ne 0) { throw "Launcher compilation failed: $taskEntry" }
}
& (Join-Path $taskStageRuntime 'python.exe') -E -s -B -X utf8 -c 'import ssl,sqlite3,tkinter,venv,ensurepip,ctypes; print("Standard runtime OK; Tk " + str(tkinter.TkVersion))'
if ($LASTEXITCODE -ne 0) { throw 'Runtime validation failed.' }
$taskFiles = @(Get-ChildItem -LiteralPath $taskStage -Recurse -File -Force)
foreach ($taskFile in $taskFiles) {
    $taskRelative = $taskFile.FullName.Substring($taskStage.Length + 1)
    if ($taskRelative -match '(^|[\\/])(site-packages|portable_data|models|development|datasets|outputs|capabilities|training-tools|__pycache__|secrets|credentials)([\\/]|$)' -or
        $taskFile.Extension -in @('.gguf','.safetensors','.onnx','.pt','.pth','.sqlite3','.db','.key','.dpapi','.secret') -or
        $taskFile.Name -in @('storage.json','model-location.txt','config.json','generation_config.json','tokenizer.json','pytorch_model.bin') -or
        ($taskFile.Extension -notin @('.py','.pyi') -and $taskFile.Name -match $taskPrivatePattern)) { throw "Private/model file in payload: $taskRelative" }
}
$taskReport = [ordered]@{version=$taskVersion;created=(Get-Date -Format o);source=$SourceRoot;stage=$taskStage;stage_bytes=($taskFiles|Measure-Object Length -Sum).Sum;runtime_bytes=(Get-ChildItem -LiteralPath $taskStageRuntime -Recurse -File|Measure-Object Length -Sum).Sum;file_count=$taskFiles.Count;entry_points=@('Free AI.exe','Free AI Trainer.exe');excludes_models_and_user_data=$true;excludes_api_configuration_and_secrets=$true;excludes_model_location=$true;downloads_during_install=$false;prepared_only=[bool]$PrepareOnly;installer=$null;installer_bytes=$null;sha256=$null}
if (-not $PrepareOnly) {
    if (-not (Test-Path -LiteralPath $Compiler -PathType Leaf)) { throw "Inno compiler unavailable: $Compiler. Payload prepared at $taskStage" }
    New-Item -ItemType Directory -Path $ReleaseDirectory -Force | Out-Null
    & $Compiler /Qp "/DSourceRoot=$SourceRoot" "/DStageRoot=$taskStage" "/DReleaseDir=$ReleaseDirectory" (Join-Path $taskDevelopment 'installer\free-ai-lite.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
    $taskInstaller = Join-Path $ReleaseDirectory ('FreeAI-Setup-' + $taskVersion + '.exe')
    $taskReport.installer=$taskInstaller
    $taskReport.installer_bytes=(Get-Item -LiteralPath $taskInstaller).Length
    $taskReport.sha256=(Get-FileHash -LiteralPath $taskInstaller -Algorithm SHA256).Hash
    Set-Content -LiteralPath ($taskInstaller + '.sha256') -Value ($taskReport.sha256 + ' *' + (Split-Path -Leaf $taskInstaller)) -Encoding ascii
}
$taskReport | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $taskReports 'latest-build.json') -Encoding utf8
$taskReport | ConvertTo-Json -Depth 6
