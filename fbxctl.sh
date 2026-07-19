#!/usr/bin/env bash
# Pilotage du moniteur Freebox en conteneur.
# Usage : ./fbxctl.sh {enroll|start|stop|restart|status|logs}
# Fonctionne avec Docker et Podman.
set -euo pipefail

cd "$(dirname "$0")"

# Docker en priorite s'il repond, sinon Podman
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    engine=docker
elif command -v podman >/dev/null 2>&1; then
    engine=podman
else
    echo "ERREUR : ni docker ni podman disponible sur cette machine" >&2
    exit 1
fi

exige_config() {
    if [ ! -f config.json ]; then
        echo "ERREUR : config.json introuvable ; copiez config.example.json" >&2
        echo "et renseignez sms.user / sms.pass d'abord." >&2
        exit 1
    fi
    # Docker (rootful) : le bind mount preserve les UID de l'hote, et le
    # conteneur tourne en fbxmon (UID 10001) ; un config.json en 600
    # appartenant a un autre utilisateur lui est donc illisible.
    [ "$engine" = docker ] || return 0
    local owner perms
    owner=$(stat -c %u config.json 2>/dev/null) || return 0
    perms=$(stat -c %a config.json 2>/dev/null) || return 0
    if [ "$owner" != "10001" ] && [ $((8#$perms & 8#044)) -eq 0 ]; then
        echo "ERREUR : config.json (mode $perms, uid $owner) est illisible pour" >&2
        echo "l'utilisateur fbxmon (UID 10001) du conteneur." >&2
        echo "Correctif : sudo chown 10001:10001 config.json   (garder le chmod 600)" >&2
        exit 1
    fi
}

case "${1:-}" in
    enroll)
        # Enrolement one-shot aupres de la box : config montee EN ECRITURE
        # (l'app_token y sera ecrit), entrypoint contourne.
        # Validation PHYSIQUE en facade de la Freebox pendant l'execution !
        exige_config
        "$engine" compose build
        extra=()
        if [ "$engine" = podman ] && \
           [ "$("$engine" info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = "true" ]; then
            extra+=(--userns=keep-id)
        fi
        "$engine" run --rm -it "${extra[@]}" \
            -v "$PWD/config.json:/config/config.json" \
            --entrypoint python3 fbx-monitor /app/fbx_enroll.py --config /config/config.json
        ;;
    start)
        exige_config
        # --build : reprend une eventuelle modif des scripts sans etape a part
        "$engine" compose up -d --build
        echo "Moniteur demarre ($engine). Logs : $0 logs"
        ;;
    stop)
        # down conserve le volume d'etat : pas de nouvelle baseline au retour.
        # Apres un stop, le conteneur ne revient PAS au boot (voulu) : refaire start.
        "$engine" compose down
        echo "Moniteur arrete."
        ;;
    restart)
        # La config etant montee depuis l'hote, un restart suffit a la relire
        "$engine" compose restart
        echo "Moniteur redemarre."
        ;;
    status)
        line=$("$engine" ps --filter name=fbx-monitor --format '{{.Names}}: {{.Status}}')
        if [ -n "$line" ]; then
            echo "$line"
            echo "--- 10 dernieres lignes de log ---"
            "$engine" logs --tail 10 fbx-monitor
        else
            echo "fbx-monitor : ARRETE"
            exit 3
        fi
        ;;
    logs)
        exec "$engine" logs -f fbx-monitor
        ;;
    *)
        echo "Usage : $0 {enroll|start|stop|restart|status|logs}" >&2
        exit 2
        ;;
esac
