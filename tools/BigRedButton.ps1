# GRIDIRON-SIM  ::  Big Red Button
# One press: pull the latest code, commit everything new in D:\gridiron-sim (except _standings\ and .zip files,
# and anything over GitHub's 100 MB limit), and push it to GitHub.
# The falling characters are decoration. The green log at the bottom shows the real steps.
# Launch with BigRedButton.bat (same folder).

Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase

$repo = Split-Path -Parent $PSScriptRoot
$git = (Get-Command git -ErrorAction SilentlyContinue).Source
if (-not $git) {
    $git = (Get-ChildItem "$env:LOCALAPPDATA\GitHubDesktop\app-*\resources\app\git\cmd\git.exe" -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -First 1).FullName
}

[xml]$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="GRIDIRON-SIM :: Big Red Button" Width="560" Height="700" Background="Black"
        WindowStartupLocation="CenterScreen" ResizeMode="CanMinimize">
  <Grid>
    <Canvas x:Name="Rain" ClipToBounds="True" Opacity="0.35"/>
    <StackPanel VerticalAlignment="Top" Margin="0,28,0,0">
      <TextBlock Text="GRIDIRON-SIM" Foreground="#FF39FF14" FontFamily="Consolas" FontSize="30" FontWeight="Bold" HorizontalAlignment="Center">
        <TextBlock.Effect><DropShadowEffect Color="#FF39FF14" BlurRadius="18" ShadowDepth="0"/></TextBlock.Effect>
      </TextBlock>
      <TextBlock x:Name="Sub" Text="press to sync with GitHub" Foreground="#FF1FA51F" FontFamily="Consolas" FontSize="14" HorizontalAlignment="Center" Margin="0,6,0,0"/>
    </StackPanel>
    <Grid x:Name="BtnGrid" Width="230" Height="230" HorizontalAlignment="Center" VerticalAlignment="Top" Margin="0,120,0,0" Cursor="Hand" RenderTransformOrigin="0.5,0.5">
      <Grid.RenderTransform><ScaleTransform x:Name="BtnScale" ScaleX="1" ScaleY="1"/></Grid.RenderTransform>
      <Ellipse x:Name="Glow" Fill="#FFFF1A1A" Margin="-34" Opacity="0.6" IsHitTestVisible="False">
        <Ellipse.Effect><BlurEffect Radius="45"/></Ellipse.Effect>
      </Ellipse>
      <Ellipse x:Name="Btn" Stroke="#FF4A0000" StrokeThickness="7">
        <Ellipse.Fill>
          <RadialGradientBrush GradientOrigin="0.35,0.3">
            <GradientStop Color="#FFFF7070" Offset="0"/>
            <GradientStop Color="#FFE00000" Offset="0.55"/>
            <GradientStop Color="#FF6A0000" Offset="1"/>
          </RadialGradientBrush>
        </Ellipse.Fill>
        <Ellipse.Effect><DropShadowEffect Color="Red" BlurRadius="60" ShadowDepth="0" Opacity="1"/></Ellipse.Effect>
      </Ellipse>
      <TextBlock x:Name="BtnText" Text="ENGAGE" Foreground="White" FontFamily="Consolas" FontSize="32" FontWeight="Bold"
                 HorizontalAlignment="Center" VerticalAlignment="Center" IsHitTestVisible="False">
        <TextBlock.Effect><DropShadowEffect Color="Black" BlurRadius="8" ShadowDepth="0"/></TextBlock.Effect>
      </TextBlock>
    </Grid>
    <Border VerticalAlignment="Bottom" Height="230" Margin="16" Background="#DD000000" BorderBrush="#FF0F6F0F" BorderThickness="1">
      <ScrollViewer x:Name="Scroll" VerticalScrollBarVisibility="Auto">
        <TextBlock x:Name="Log" Foreground="#FF39FF14" FontFamily="Consolas" FontSize="12" TextWrapping="Wrap" Margin="8"/>
      </ScrollViewer>
    </Border>
  </Grid>
</Window>
"@

$win = [Windows.Markup.XamlReader]::Load((New-Object System.Xml.XmlNodeReader $xaml))
$rain = $win.FindName("Rain"); $glow = $win.FindName("Glow"); $btnGrid = $win.FindName("BtnGrid")
$btnText = $win.FindName("BtnText"); $btnScale = $btnGrid.RenderTransform; $sub = $win.FindName("Sub")
$log = $win.FindName("Log"); $scroll = $win.FindName("Scroll")

# ---------------------------------------------------------------- glow pulse
function Set-Pulse([double]$seconds) {
    $a = New-Object System.Windows.Media.Animation.DoubleAnimation(0.3, 0.95, [TimeSpan]::FromSeconds($seconds))
    $a.AutoReverse = $true
    $a.RepeatBehavior = [System.Windows.Media.Animation.RepeatBehavior]::Forever
    $glow.BeginAnimation([System.Windows.UIElement]::OpacityProperty, $a)
}
Set-Pulse 1.2

# ---------------------------------------------------------------- matrix rain (decoration)
$chars = "0123456789ABCDEF<>/\|=+*#%@QBWRTEYXZ".ToCharArray()
$rng = New-Object System.Random
$cols = @()
$colW = 16
$nCols = [int](560 / $colW)
function New-Stream($len) { (1..$len | ForEach-Object { $chars[$rng.Next($chars.Length)] }) -join "`n" }
for ($i = 0; $i -lt $nCols; $i++) {
    $len = $rng.Next(8, 26)
    $tb = New-Object System.Windows.Controls.TextBlock
    $tb.FontFamily = "Consolas"; $tb.FontSize = 14; $tb.Text = New-Stream $len
    $brush = New-Object System.Windows.Media.LinearGradientBrush
    $brush.StartPoint = "0,0"; $brush.EndPoint = "0,1"
    $brush.GradientStops.Add((New-Object System.Windows.Media.GradientStop([System.Windows.Media.Color]::FromArgb(0, 57, 255, 20), 0)))
    $brush.GradientStops.Add((New-Object System.Windows.Media.GradientStop([System.Windows.Media.Color]::FromArgb(255, 57, 255, 20), 0.85)))
    $brush.GradientStops.Add((New-Object System.Windows.Media.GradientStop([System.Windows.Media.Color]::FromArgb(255, 220, 255, 220), 1)))
    $tb.Foreground = $brush
    [System.Windows.Controls.Canvas]::SetLeft($tb, $i * $colW)
    $y = - $rng.Next(0, 900)
    [System.Windows.Controls.Canvas]::SetTop($tb, $y)
    [void]$rain.Children.Add($tb)
    $cols += [pscustomobject]@{ tb = $tb; y = [double]$y; speed = 2 + $rng.NextDouble() * 5; len = $len }
}

# ---------------------------------------------------------------- the real work (background runspace)
$q = New-Object 'System.Collections.Concurrent.ConcurrentQueue[string]'
$state = [hashtable]::Synchronized(@{ running = $false; done = $false; ok = $true })
$work = {
    param($repo, $git, $q, $state)
    function Say($m) { $q.Enqueue($m) }
    function G([string[]]$a) {
        $out = & $git -C $repo @a 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
        foreach ($l in $out) { if ($l.Trim()) { Say ("    " + $l) } }
        return $code
    }
    try {
        if (-not $git) { throw "git not found - is GitHub Desktop installed?" }
        Say "> repository: $repo"
        Say "> pulling the latest code from GitHub"
        if ((G @('pull', '--rebase', '--autostash')) -ne 0) { throw "pull failed - open GitHub Desktop to see why" }
        Say "> scanning for new or changed files"
        [void](G @('add', '-A', '--', '.', ':!_standings', ':!*.zip'))
        $staged = & $git -C $repo diff --cached --name-only --diff-filter=AM
        foreach ($f in $staged) {
            $p = Join-Path $repo $f
            if ((Test-Path $p) -and ((Get-Item $p).Length -gt 95MB)) {
                & $git -C $repo reset -q HEAD -- $f
                Say "    skipped $f (bigger than GitHub's 100 MB limit)"
            }
        }
        $files = @(& $git -C $repo diff --cached --name-only)
        if ($files.Count -eq 0) { Say "> nothing new to commit" }
        else {
            foreach ($f in $files) { Say "    + $f" }
            Say ("> committing " + $files.Count + " file(s)")
            if ((G @('commit', '-m', ("sync: Big Red Button " + (Get-Date -Format 'yyyy-MM-dd HH:mm')))) -ne 0) { throw "commit failed" }
        }
        Say "> pushing to GitHub"
        if ((G @('push')) -ne 0) { throw "push failed - open GitHub Desktop and click Push origin" }
        Say "> ALL SYSTEMS GO"
    } catch {
        $state.ok = $false
        Say ("!! " + $_.Exception.Message)
    } finally {
        $state.done = $true
    }
}

$pending = New-Object System.Text.StringBuilder
$boost = 1.0
$finishAt = $null

function Start-Run {
    if ($state.running) { return }
    $state.running = $true; $state.done = $false; $state.ok = $true
    $log.Text = ""; [void]$pending.Clear()
    $btnText.Text = "RUNNING"; $sub.Text = "establishing uplink..."
    $script:boost = 4.0; $rain.Opacity = 0.9; Set-Pulse 0.25
    $ps = [PowerShell]::Create()
    [void]$ps.AddScript($work).AddArgument($repo).AddArgument($git).AddArgument($q).AddArgument($state)
    [void]$ps.BeginInvoke()
}

$btnGrid.Add_MouseLeftButtonDown({ $btnScale.ScaleX = 0.92; $btnScale.ScaleY = 0.92 })
$btnGrid.Add_MouseLeftButtonUp({ $btnScale.ScaleX = 1; $btnScale.ScaleY = 1; Start-Run })

# ---------------------------------------------------------------- frame timer: rain + typewriter log
$timer = New-Object System.Windows.Threading.DispatcherTimer
$timer.Interval = [TimeSpan]::FromMilliseconds(35)
$timer.Add_Tick({
    $h = $rain.ActualHeight; if ($h -le 0) { $h = 700 }
    foreach ($c in $cols) {
        $c.y += $c.speed * $script:boost
        if ($c.y -gt $h) { $c.y = - ($c.len * 16) - $rng.Next(0, 300); $c.tb.Text = New-Stream $c.len }
        elseif ($rng.Next(0, 12) -eq 0) {
            $t = $c.tb.Text.ToCharArray(); $k = 2 * $rng.Next(0, $c.len)
            if ($k -lt $t.Length) { $t[$k] = $chars[$rng.Next($chars.Length)]; $c.tb.Text = -join $t }
        }
        [System.Windows.Controls.Canvas]::SetTop($c.tb, $c.y)
    }
    $line = $null
    while ($q.TryDequeue([ref]$line)) { [void]$pending.Append($line + "`n") }
    if ($pending.Length -gt 0) {
        $n = [Math]::Min(4, $pending.Length)
        $log.Text += $pending.ToString(0, $n); [void]$pending.Remove(0, $n)
        $scroll.ScrollToEnd()
    }
    if ($state.running -and $state.done -and $pending.Length -eq 0) {
        $state.running = $false
        if ($state.ok) { $btnText.Text = "DONE"; $sub.Text = "synced - everything is on GitHub" }
        else { $btnText.Text = "ERROR"; $sub.Text = "see the log below" }
        $script:finishAt = (Get-Date).AddSeconds(2.5)
    }
    if ($script:finishAt -and (Get-Date) -gt $script:finishAt) {
        $script:finishAt = $null; $script:boost = 1.0; $rain.Opacity = 0.35; Set-Pulse 1.2
        if ($btnText.Text -eq "DONE") { $btnText.Text = "ENGAGE" }
    }
})
$timer.Start()

if (-not $git) { $log.Text = "!! git not found - install GitHub Desktop, then reopen this window`n" }
else { $log.Text = "> standing by. repository: $repo`n" }
[void]$win.ShowDialog()
