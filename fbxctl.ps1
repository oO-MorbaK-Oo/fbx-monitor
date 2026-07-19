#!/usr/bin/env pwsh
# Pilotage du moniteur Freebox en conteneur - version Windows (PowerShell).
# Equivalent de fbxctl.sh. Usage : .\fbxctl.ps1 {enroll|start|stop|restart|status|logs}
# Fonctionne avec Docker Desktop comme avec Podman Desktop.
#
# Sous Windows, les conteneurs tournent dans une VM Linux (WSL2 / podman
# machine) : les fichiers montes y arrivent avec des permissions larges, donc
# pas de chown/UID a verifier ni de --userns=keep-id a ajouter (contrairement
# a fbxctl.sh cote Unix). Voir la section Windows du README.

Set-Location $PSScriptRoot

# Docker en priorite s'il repond, sinon Podman
function Get-Engine {
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        docker info *> $null
        if ($LASTEXITCODE -eq 0) { return 'docker' }
    }
    if (Get-Command podman -ErrorAction SilentlyContinue) { return 'podman' }
    Write-Error "ni docker ni podman disponible (ou le moteur ne repond pas)"
    exit 1
}
$engine = Get-Engine

function Test-Config {
    if (-not (Test-Path config.json)) {
        Write-Error "config.json introuvable ; copiez config.example.json et renseignez sms.user / sms.pass d'abord."
        exit 1
    }
    # Pas de verification de permissions : sous Windows le montage arrive dans
    # la VM avec des droits larges, le conteneur lit le fichier sans mapping.
}

$cmd = if ($args.Count -ge 1) { $args[0] } else { '' }

switch ($cmd) {
    'enroll' {
        # Enrolement one-shot : config montee EN ECRITURE (l'app_token y sera
        # ecrit), entrypoint contourne. Validation PHYSIQUE en facade de la
        # Freebox pendant l'execution !
        Test-Config
        & $engine compose build
        & $engine run --rm -it `
            -v "${PWD}\config.json:/config/config.json" `
            --entrypoint python3 fbx-monitor /app/fbx_enroll.py --config /config/config.json
    }
    'start' {
        Test-Config
        # --build : reprend une eventuelle modif des scripts sans etape a part
        & $engine compose up -d --build
        Write-Host "Moniteur demarre ($engine). Logs : .\fbxctl.ps1 logs"
    }
    'stop' {
        # down conserve le volume d'etat : pas de nouvelle baseline au retour.
        & $engine compose down
        Write-Host "Moniteur arrete."
    }
    'restart' {
        # La config etant montee depuis l'hote, un restart suffit a la relire
        & $engine compose restart
        Write-Host "Moniteur redemarre."
    }
    'status' {
        $line = & $engine ps --filter name=fbx-monitor --format '{{.Names}}: {{.Status}}'
        if ($line) {
            Write-Host $line
            Write-Host "--- 10 dernieres lignes de log ---"
            & $engine logs --tail 10 fbx-monitor
        } else {
            Write-Host "fbx-monitor : ARRETE"
            exit 3
        }
    }
    'logs' {
        & $engine logs -f fbx-monitor
    }
    default {
        Write-Host "Usage : .\fbxctl.ps1 {enroll|start|stop|restart|status|logs}"
        exit 2
    }
}
