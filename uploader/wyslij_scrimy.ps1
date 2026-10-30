<#
  LP Tracker: wysyła scrimy z klienta League of Legends do bota drużyny.

  Czyta ostatnie gry niestandardowe z historii meczów klienta LoL (lokalne API klienta,
  tylko odczyt, tak jak Porofessor czy Blitz) i wysyła je przez webhook Discorda.
  Bot dopisuje mapy do arkusza drużyny i sam pomija gry, które już ma, więc wysłanie
  czegoś drugi raz niczego nie dubluje.

  Uruchamianie: "Wyslij scrimy.cmd" (raz) albo "Wysylaj automatycznie.cmd" (co kilka minut).
#>
param(
    # Sprawdzaj co kilka minut, zamiast raz
    [switch]$Watch,
    [int]$IntervalMinutes = 5,
    # Wyślij też gry wysłane wcześniej (bot i tak pominie te, które ma)
    [switch]$All
)

$ErrorActionPreference = 'Stop'
$UploadFormat = 'lp-tracker-scrims'
$UploadFileName = 'lp-tracker-scrims.json'
# Timelines make each game bigger; a Discord webhook upload can be at most 10 MB
$GamesPerUpload = 5
$HistorySize = 20
$SummonersRift = 11
$DataDir = Join-Path $env:APPDATA 'LP Tracker'
$WebhookFile = Join-Path $DataDir 'webhook.txt'
$SentFile = Join-Path $DataDir 'wyslane.txt'

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.Net.Http
if (-not ('LpTrackerHttp' -as [type])) {
    Add-Type -ReferencedAssemblies System.Net.Http -TypeDefinition @'
using System.Net.Http;
public static class LpTrackerHttp {
    // The LoL client serves its local API (127.0.0.1 only) with a self-signed certificate
    public static HttpClient LocalClient() {
        var handler = new HttpClientHandler();
        handler.ServerCertificateCustomValidationCallback = (message, cert, chain, errors) => true;
        return new HttpClient(handler);
    }
}
'@
}

function Get-WebhookUrl {
    if (Test-Path $WebhookFile) {
        $url = (Get-Content $WebhookFile -Raw).Trim()
        if ($url) { return $url }
    }
    Write-Host 'Pierwsze uruchomienie: wklej adres webhooka z kanału Discorda na scrimy.'
    $url = (Read-Host 'Adres webhooka').Trim()
    if ($url -notmatch '^https://(ptb\.|canary\.)?discord(app)?\.com/api/webhooks/\d+/[\w-]+') {
        throw 'To nie wygląda na adres webhooka Discorda (https://discord.com/api/webhooks/...).'
    }
    Set-Content -Path $WebhookFile -Value $url -Encoding UTF8
    return $url
}

function Get-LeagueClient {
    # Port and password of the client's local API: from the running client's command line,
    # or from the lockfile in the game folder
    $port = $null
    $token = $null
    $process = Get-CimInstance Win32_Process -Filter "Name = 'LeagueClientUx.exe'" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($process -and $process.CommandLine) {
        $port = [regex]::Match($process.CommandLine, '--app-port=(\d+)').Groups[1].Value
        $token = [regex]::Match($process.CommandLine, '--remoting-auth-token=([^\s"]+)').Groups[1].Value
    }
    if ($process -and (-not $port -or -not $token)) {
        foreach ($dir in @('C:\Riot Games\League of Legends', 'D:\Riot Games\League of Legends')) {
            $lockfile = Join-Path $dir 'lockfile'
            if (Test-Path $lockfile) {
                $parts = (Get-Content $lockfile -Raw).Split(':')  # name:pid:port:password:protocol
                $port = $parts[2]
                $token = $parts[3]
                break
            }
        }
    }
    if (-not $port -or -not $token) { return $null }
    $auth = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("riot:$token"))
    return @{ Base = "https://127.0.0.1:$port"; Auth = "Basic $auth" }
}

function Invoke-Lcu($Lcu, [string]$Path) {
    $request = New-Object System.Net.Http.HttpRequestMessage -ArgumentList ([System.Net.Http.HttpMethod]::Get), ($Lcu.Base + $Path)
    $request.Headers.TryAddWithoutValidation('Authorization', $Lcu.Auth) | Out-Null
    $response = $script:LocalHttp.SendAsync($request).GetAwaiter().GetResult()
    $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    if (-not $response.IsSuccessStatusCode) {
        throw "Klient LoL odpowiedział $([int]$response.StatusCode) na $Path"
    }
    return $body
}

function Get-CustomGameIds($Lcu) {
    $history = Invoke-Lcu $Lcu "/lol-match-history/v1/products/lol/current-summoner/matches?begIndex=0&endIndex=$($HistorySize - 1)" |
        ConvertFrom-Json
    # Older and newer clients nest the list differently
    $games = $history.games.games
    if ($null -eq $games) { $games = $history.games }
    return @($games |
        Where-Object { $_.gameType -eq 'CUSTOM_GAME' -and $_.mapId -eq $SummonersRift } |
        ForEach-Object { [string]$_.gameId })
}

function Get-GameTimeline($Lcu, [string]$GameId) {
    # Minute-by-minute data of the game (gold, CS, positions, kills) as '"<gameId>":{...}', or $null
    try {
        return '"' + $GameId + '":' + (Invoke-Lcu $Lcu "/lol-match-history/v1/game-timelines/$GameId")
    }
    catch {
        Write-Host "Brak przebiegu gry $GameId ($($_.Exception.Message)), wysyłam ją bez niego."
        return $null
    }
}

function Send-Games([string]$WebhookUrl, [string[]]$GameJsons, [string[]]$TimelineJsons, [string]$Uploader) {
    # Game JSON is passed through as the client returned it, so nothing gets cut
    $json = '{"format":"' + $UploadFormat + '","version":2,"uploader":' + (ConvertTo-Json $Uploader) +
        ',"games":[' + ($GameJsons -join ',') + '],"timelines":{' + ($TimelineJsons -join ',') + '}}'
    $payload = ConvertTo-Json @{ content = "Scrimy z klienta LoL ($Uploader): $($GameJsons.Count) gier" } -Compress

    $content = New-Object System.Net.Http.MultipartFormDataContent
    $content.Add((New-Object System.Net.Http.StringContent -ArgumentList $payload, ([Text.Encoding]::UTF8), 'application/json'), 'payload_json')
    $file = New-Object System.Net.Http.ByteArrayContent -ArgumentList (, [Text.Encoding]::UTF8.GetBytes($json))
    $file.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse('application/json')
    $content.Add($file, 'files[0]', $UploadFileName)

    $separator = if ($WebhookUrl.Contains('?')) { '&' } else { '?' }
    $response = $script:WebHttp.PostAsync("$WebhookUrl${separator}wait=true", $content).GetAwaiter().GetResult()
    if (-not $response.IsSuccessStatusCode) {
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        throw "Discord odrzucił wysyłkę ($([int]$response.StatusCode)): $body"
    }
}

function Send-NewScrims([string]$WebhookUrl, [bool]$Quiet) {
    $lcu = Get-LeagueClient
    if (-not $lcu) {
        if (-not $Quiet) { Write-Host 'Klient League of Legends nie jest włączony. Włącz go i spróbuj ponownie.' }
        return
    }
    $me = Invoke-Lcu $lcu '/lol-summoner/v1/current-summoner' | ConvertFrom-Json
    $uploader = "$($me.gameName)#$($me.tagLine)"

    $sent = @{}
    if (-not $All -and (Test-Path $SentFile)) {
        Get-Content $SentFile | ForEach-Object { $sent[$_.Trim()] = $true }
    }
    $ids = @(Get-CustomGameIds $lcu | Where-Object { -not $sent.ContainsKey($_) })
    if ($ids.Count -eq 0) {
        if (-not $Quiet) { Write-Host "Brak nowych gier niestandardowych w historii konta $uploader." }
        return
    }

    Write-Host "$(Get-Date -Format 'HH:mm') Nowe gry niestandardowe: $($ids.Count), wysyłam..."
    for ($i = 0; $i -lt $ids.Count; $i += $GamesPerUpload) {
        $batch = @($ids[$i..([Math]::Min($i + $GamesPerUpload, $ids.Count) - 1)])
        $jsons = @($batch | ForEach-Object { Invoke-Lcu $lcu "/lol-match-history/v1/games/$_" })
        $timelines = @($batch | ForEach-Object { Get-GameTimeline $lcu $_ } | Where-Object { $_ })
        Send-Games $WebhookUrl $jsons $timelines $uploader
        Add-Content -Path $SentFile -Value $batch
        Write-Host "Wysłano $($batch.Count) gier. Bot odpisze na kanale, co dodał do arkusza."
    }
}

$WebhookUrl = Get-WebhookUrl
$script:LocalHttp = [LpTrackerHttp]::LocalClient()
$script:WebHttp = New-Object System.Net.Http.HttpClient

if ($Watch) {
    Write-Host "Tryb automatyczny: sprawdzam historię co $IntervalMinutes min. Zamknij to okno, żeby zakończyć."
    while ($true) {
        try { Send-NewScrims $WebhookUrl $true } catch { Write-Host "$(Get-Date -Format 'HH:mm') Błąd: $($_.Exception.Message)" }
        Start-Sleep -Seconds ($IntervalMinutes * 60)
    }
}
else {
    try { Send-NewScrims $WebhookUrl $false }
    catch {
        Write-Host "Błąd: $($_.Exception.Message)"
        exit 1
    }
}
