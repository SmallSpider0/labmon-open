<#
labmon：Windows 待装更新（离线搜索，#166）。只读：不下载、不安装、不改任何设置。

  两处跑的是这同一个文件：
  - 看板服务的体检（labmon/hostcheck.py）在 WSL 里经 powershell.exe 跑它（不带 -Out），读标准输出的一行 JSON；
  - 服务账号做不了离线搜索时的退路：SYSTEM 计划任务带 -Out 跑它，写 C:\ProgramData\labmon\wu.json，体检去读这个文件
    （install-wu-task.ps1 建任务，由维护者用管理员跑）。

  输出：{"t": epoch 秒, "updates": [{"id", "title", "kb", "sec", "driver"}], "error": null 或原因}
  离线搜索（Online = $false）只查本机已同步的更新目录，不连微软服务器；目录由系统的更新服务按计划自己刷新。
#>
param([string]$Out)
$ErrorActionPreference = 'Stop'
$secCat = '0fa1201d-4330-4fa8-8ae9-b877473b6441'          # 分类“Security Updates”的 CategoryID（与界面语言无关）
$now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
try {
  $s = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher()
  $s.Online = $false
  $r = $s.Search('IsInstalled=0 and IsHidden=0')
  $list = @(foreach ($u in $r.Updates) {
    $cats = @(foreach ($c in $u.Categories) { $c.CategoryID })
    [ordered]@{
      id = [string]$u.Identity.UpdateID
      title = [string]$u.Title
      kb = (@($u.KBArticleIDs) -join ',')
      sec = [bool]($u.MsrcSeverity -or ($cats -contains $secCat))
      driver = ($u.Type -eq 2)
    }
  })
  $res = [ordered]@{ t = $now; updates = $list; error = $null }
} catch {
  $res = [ordered]@{ t = $now; updates = $null; error = [string]$_.Exception.Message }
}
$json = ConvertTo-Json -InputObject $res -Compress -Depth 4
if ($Out) {
  $dir = Split-Path -Parent $Out
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
  $tmp = "$Out.tmp"
  [IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding $false))   # 无 BOM 的 UTF-8；先写临时文件再换名，读的一方不会读到半截
  Move-Item -Force -LiteralPath $tmp -Destination $Out
} else {
  $json
}
