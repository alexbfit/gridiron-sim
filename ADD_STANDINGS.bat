@echo off
title Adding week 5 and 6 standings
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
 "$ErrorActionPreference='Stop';" ^
 "$dest='%~dp0data\ownership';" ^
 "$roots=@(\"$env:USERPROFILE\Downloads\",'D:\Downloads',\"$env:USERPROFILE\Desktop\",\"$env:USERPROFILE\Documents\",'D:\Alex') | Where-Object { Test-Path $_ };" ^
 "$want=@{'196403753'='DK-2026-05-main_playaction_standings.csv';'196653903'='DK-2026-06-main_playaction_standings.csv'};" ^
 "foreach($id in $want.Keys){" ^
 "  $out=Join-Path $dest $want[$id];" ^
 "  if(Test-Path $out){ Write-Host ('already there: '+$want[$id]); continue }" ^
 "  $zip=Get-ChildItem -Path $roots -Recurse -Depth 2 -Filter ('contest-standings-'+$id+'*.zip') -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1;" ^
 "  $csv=Get-ChildItem -Path $roots -Recurse -Depth 2 -Filter ('contest-standings-'+$id+'*.csv') -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1;" ^
 "  if($csv){ Copy-Item $csv.FullName $out; Write-Host ('added '+$want[$id]+' from '+$csv.FullName); continue }" ^
 "  if(-not $zip){ Write-Host ('NOT FOUND: contest-standings-'+$id+'.zip - download it again from DraftKings into Downloads and re-run this file') -ForegroundColor Red; continue }" ^
 "  $tmp=Join-Path $env:TEMP ('st_'+$id); if(Test-Path $tmp){Remove-Item $tmp -Recurse -Force};" ^
 "  Expand-Archive -Path $zip.FullName -DestinationPath $tmp -Force;" ^
 "  $c=Get-ChildItem $tmp -Filter *.csv | Select-Object -First 1;" ^
 "  Move-Item $c.FullName $out -Force; Remove-Item $tmp -Recurse -Force;" ^
 "  Write-Host ('added '+$want[$id]+' from '+$zip.FullName) -ForegroundColor Green }"
echo.
echo Done. Now in GitHub Desktop: Commit to main, then Push origin.
pause
(goto) 2>nul & del "%~f0"
