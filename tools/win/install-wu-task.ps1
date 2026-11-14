<#
labmon：给看板服务的体检装“待装更新”的退路（#166）。只在服务账号做不了离线搜索时才装（先在 WSL 里跑 labmon hostcheck 看“待装更新”那一行）。

  由维护者在 dashboard-host 上用“以管理员身份运行”的 PowerShell 跑：
      powershell -NoProfile -ExecutionPolicy Bypass -File install-wu-task.ps1 [-Source <labmon-wu.ps1 的路径>]
  做三件事：
  1. 把 labmon-wu.ps1 拷到 C:\ProgramData\labmon\（缺省取与本脚本同一目录下的那份）；
  2. 这个目录只许 SYSTEM 与管理员改、普通用户（含服务账号）只读：计划任务以 SYSTEM 跑里面的脚本，别人改得了就等于拿到 SYSTEM；
     目录已经在、却不是 SYSTEM 或管理员建的（或里面有别人的文件、子目录、链接），报错停下，看过、删掉再跑；
  3. 建计划任务 labmon-wu：以 SYSTEM 运行，开机时与之后每 6 小时各跑一次，写 C:\ProgramData\labmon\wu.json，然后立刻跑一次。
  再跑一次本脚本就是更新（覆盖脚本、重建任务）。卸载：Unregister-ScheduledTask labmon-wu；删 C:\ProgramData\labmon。
#>
param([string]$Source = (Join-Path $PSScriptRoot 'labmon-wu.ps1'))
$ErrorActionPreference = 'Stop'
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw '要用“以管理员身份运行”的 PowerShell 跑'
}
$dir = 'C:\ProgramData\labmon'
$script = Join-Path $dir 'labmon-wu.ps1'
$out = Join-Path $dir 'wu.json'
$trusted = @('S-1-5-18', 'S-1-5-32-544')                   # SYSTEM、Administrators（用 SID，与界面语言无关）
$owners = $trusted + $id.User.Value                       # 认作可信的所有者：再加上跑本脚本的管理员自己（“对象创建者”策略下以前装的归它）
$admins = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
$reparse = [IO.FileAttributes]::ReparsePoint
function OwnerOf($p) { (Get-Acl -LiteralPath $p).GetOwner([Security.Principal.SecurityIdentifier]).Value }

# 已有的目录只认 SYSTEM 或管理员建的：普通用户先建的，所有者隐含 WRITE_DAC，能把权限改回来、换掉 SYSTEM 要跑的脚本（等于拿到 SYSTEM）。
# 不认的不替它收拾（里面可能有链接），停下让维护者看过、删掉再跑。不存在就新建，不带 -Force：别人抢先建了也报错停下。
$fix = "先看清里面是什么、删掉 $dir，再跑本脚本"
if (Test-Path -LiteralPath $dir) {
  $it = Get-Item -LiteralPath $dir -Force
  if (-not $it.PSIsContainer -or ($it.Attributes -band $reparse)) { throw "$dir 不是普通目录（是文件或链接）：$fix" }
  $own = OwnerOf $dir
  if ($owners -notcontains $own) { throw "$dir 已存在，所有者是 $own（不是 SYSTEM 或管理员）：$fix" }
} else {
  New-Item -ItemType Directory -Path $dir | Out-Null
}

# 目录的权限：所有者定为 Administrators；不继承 ProgramData 的（那边普通用户能建文件），SYSTEM 与管理员完全控制，Users 只读
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetOwner($admins)
$acl.SetAccessRuleProtection($true, $false)
$inh = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
$none = [Security.AccessControl.PropagationFlags]::None
foreach ($who in $trusted) {
  $sid = New-Object Security.Principal.SecurityIdentifier($who)
  $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', $inh, $none, 'Allow')))
}
$users = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-545')
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($users, 'ReadAndExecute', $inh, $none, 'Allow')))
Set-Acl -LiteralPath $dir -AclObject $acl                    # 已有的文件随之改成继承这一份
if ((OwnerOf $dir) -ne $admins.Value) { throw "没能把 $dir 的所有者改成 Administrators：$fix" }

# 里面已有的东西逐个核对：只许 SYSTEM 或管理员的普通文件（别人的文件同样隐含 WRITE_DAC；子目录与链接一概不认）
$bad = @(Get-ChildItem -LiteralPath $dir -Force | Where-Object {
  $_.PSIsContainer -or ($_.Attributes -band $reparse) -or ($owners -notcontains (OwnerOf $_.FullName)) })
if ($bad.Count) { throw "$dir 里有不认的东西（子目录、链接或不是 SYSTEM / 管理员的文件）：$(($bad | ForEach-Object Name) -join '、')；$fix" }
# 文件自己的显式 ACE 优先于继承的（以前带进来的、谁加过写权限的）：逐个重置成只继承目录这一份，再核对确实没有显式 ACE
foreach ($f in Get-ChildItem -LiteralPath $dir -Force) {
  & icacls.exe $f.FullName /reset /Q | Out-Null
  if ($LASTEXITCODE) { throw "没能重置 $($f.FullName) 的权限（icacls 退出码 $LASTEXITCODE）：$fix" }
  if (@((Get-Acl -LiteralPath $f.FullName).Access | Where-Object { -not $_.IsInherited }).Count) { throw "$($f.FullName) 还带着自己的权限：$fix" }
}
Copy-Item -Force -LiteralPath $Source -Destination $script

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`" -Out `"$out`""
$boot = New-ScheduledTaskTrigger -AtStartup
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Hours 6)
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'labmon-wu' -Action $action -Trigger @($boot, $every) -Principal $principal -Settings $settings `
  -Description 'labmon 体检：离线搜索待装的 Windows 更新，写 C:\ProgramData\labmon\wu.json（#166）' -Force | Out-Null
Start-ScheduledTask -TaskName 'labmon-wu'
Write-Host "已装：计划任务 labmon-wu（SYSTEM，开机时与每 6 小时），结果写 $out。等一两分钟后在 WSL 里跑 labmon hostcheck 核对“待装更新”一行写“来自计划任务”。"
