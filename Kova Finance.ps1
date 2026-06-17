$ngrok = "C:\Users\anton\AppData\Local\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe"

# Save current AC sleep timeout and disable sleep while server runs
$pcfgOutput = powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE
$hexLine    = ($pcfgOutput | Select-String "Current AC Power Setting Index").Line
$hexValue   = ($hexLine -replace ".*0x", "").Trim()
$originalSleepSec = [Convert]::ToInt32($hexValue, 16)
$originalSleepMin = [Math]::Round($originalSleepSec / 60)
powercfg /change standby-timeout-ac 0 | Out-Null
Write-Host "Sleep disabled while server is running." -ForegroundColor DarkGray

try {
    # Kill any leftover processes
    Stop-Process -Name ngrok -ErrorAction SilentlyContinue
    Stop-Process -Name python -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1

    Write-Host ""
    Write-Host "Starting Kova Finance server..." -ForegroundColor Cyan
    $python = "C:\Users\anton\AppData\Local\Programs\Python\Python39\python.exe"
    $runpy  = "C:\Users\anton\Desktop\Kova Finance\webapp\run.py"
    Start-Process $python -ArgumentList "`"$runpy`"" -WindowStyle Minimized

    Start-Sleep -Seconds 2

    Write-Host "Starting ngrok..." -ForegroundColor Cyan
    Start-Process $ngrok -ArgumentList "http 8080" -WindowStyle Minimized

    Write-Host "Waiting for ngrok to connect..." -ForegroundColor Cyan
    $url = $null
    for ($i = 0; $i -lt 10; $i++) {
        Start-Sleep -Seconds 2
        try {
            $url = (Invoke-WebRequest -Uri http://localhost:4040/api/tunnels -UseBasicParsing | ConvertFrom-Json).tunnels[0].public_url
            if ($url) { break }
        } catch {}
    }

    Write-Host ""
    if ($url) {
        Write-Host "==========================================" -ForegroundColor Green
        Write-Host "  Kova Finance is running!" -ForegroundColor Green
        Write-Host "==========================================" -ForegroundColor Green
        Write-Host "  Local:  http://localhost:8080" -ForegroundColor White
        Write-Host "  Mobile: $url" -ForegroundColor Yellow
        Write-Host "==========================================" -ForegroundColor Green
        Write-Host ""
        Write-Host "Opening browser..." -ForegroundColor Cyan
        Start-Process "http://localhost:8080"
        Write-Host ""
        Write-Host "Paste the yellow URL into the Kova Finance app on your phone." -ForegroundColor Yellow
    } else {
        Write-Host "Could not get ngrok URL. Check your internet connection and authtoken." -ForegroundColor Red
    }

    Write-Host ""
    Write-Host "Press any key to stop the server and restore sleep settings..."
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")

} finally {
    # Restore original sleep setting
    powercfg /change standby-timeout-ac $originalSleepMin | Out-Null
    Write-Host ""
    Write-Host "Sleep settings restored." -ForegroundColor DarkGray
}
