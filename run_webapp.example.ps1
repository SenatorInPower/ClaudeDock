# ClaudeDock — web controller watchdog (example).
#
# Keeps webapp.py alive, and (optionally) a reverse SSH tunnel so you can reach
# the web UI from your phone / the Telegram Mini App. Copy to run_webapp.ps1 and
# edit the tunnel block, or delete it if you only use the web UI on this PC.
#
#   powershell -ExecutionPolicy Bypass -File run_webapp.ps1
#
$proj = $PSScriptRoot
$pyw  = "python"           # or the full path to pythonw.exe / python.exe

# ---- optional reverse tunnel (expose 127.0.0.1:<PORT> on a public server) ----
$ENABLE_TUNNEL = $false                       # set $true to use the tunnel
$LOCAL_PORT    = 8765                          # must match web_port in config.json
$REMOTE_HOST   = "user@your-server.example"    # your VPS
$REMOTE_PORT   = 9099                          # port on the VPS that forwards back
$SSH_KEY       = "$HOME\.ssh\id_ed25519"       # key with access to the VPS
# On the server, put nginx in front of 127.0.0.1:$REMOTE_PORT (TLS + the /cu/ path).

function WebAlive {
  [bool](Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
         Where-Object { $_.CommandLine -match 'webapp\.py' })
}
function TunAlive {
  [bool](Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" |
         Where-Object { $_.CommandLine -match [string]$REMOTE_PORT })
}

while ($true) {
  if (-not (WebAlive)) {
    Start-Process -FilePath $pyw -ArgumentList "`"$proj\webapp.py`"" -WorkingDirectory $proj
  }
  if ($ENABLE_TUNNEL -and -not (TunAlive)) {
    Start-Process -FilePath "ssh" -ArgumentList `
      "-i","`"$SSH_KEY`"","-N","-o","StrictHostKeyChecking=no","-o","ServerAliveInterval=30",`
      "-o","ServerAliveCountMax=3","-o","ExitOnForwardFailure=yes",`
      "-R","127.0.0.1:${REMOTE_PORT}:127.0.0.1:${LOCAL_PORT}",$REMOTE_HOST -WindowStyle Hidden
  }
  Start-Sleep -Seconds 30
}
