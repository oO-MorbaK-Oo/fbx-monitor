#!/usr/bin/env python3
"""
Enrôlement one-shot auprès de l'API Freebox.

Enregistre le script comme "application" auprès de la box et récupère
l'app_token permanent, qui est ensuite écrit dans le fichier de config.

ATTENTION : pendant l'exécution, il faut VALIDER PHYSIQUEMENT la demande
sur la façade de la Freebox (flèche droite / coche).

Usage :
    python3 fbx_enroll.py --config /etc/fbx-monitor/config.json

Aucune dépendance externe : uniquement la bibliothèque standard.
"""

import argparse
import json
import os
import socket
import sys
import time
import urllib.request
import urllib.error


def http_json(url, payload=None, timeout=10):
    """GET (payload=None) ou POST JSON, retourne le JSON décodé."""
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Enrôlement API Freebox")
    parser.add_argument("--config", default="config.json",
                        help="Chemin du fichier de configuration (défaut: ./config.json)")
    args = parser.parse_args()

    # --- Charger la config ---
    try:
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        print(f"ERREUR : fichier de config introuvable : {args.config}")
        print("Copiez config.example.json et adaptez-le d'abord.")
        sys.exit(1)

    if cfg.get("app_token"):
        print("Un app_token est déjà présent dans la config.")
        rep = input("Le remplacer par un nouveau ? [o/N] ").strip().lower()
        if rep != "o":
            print("Abandon, rien n'a été modifié.")
            sys.exit(0)

    base = cfg["freebox_url"].rstrip("/")

    # --- Découverte de la version d'API ---
    try:
        ver = http_json(base + "/api_version")
    except (urllib.error.URLError, socket.timeout) as e:
        print(f"ERREUR : impossible de joindre la Freebox sur {base} : {e}")
        print("Ce script doit être lancé depuis le LAN (ou via le VPN).")
        sys.exit(1)

    major = ver["api_version"].split(".")[0]
    api = f"{base}{ver['api_base_url']}v{major}"
    print(f"Freebox détectée : {ver.get('box_model', '?')} — API v{ver['api_version']}")

    # --- Demande d'autorisation ---
    payload = {
        "app_id": cfg["app_id"],
        "app_name": cfg["app_name"],
        "app_version": cfg["app_version"],
        "device_name": cfg.get("device_name", socket.gethostname()),
    }
    resp = http_json(api + "/login/authorize/", payload)
    if not resp.get("success"):
        print(f"ERREUR : la box a refusé la demande : {resp}")
        sys.exit(1)

    app_token = resp["result"]["app_token"]
    track_id = resp["result"]["track_id"]

    print()
    print("=" * 60)
    print(">>> VALIDEZ MAINTENANT LA DEMANDE SUR LA FACADE DE LA FREEBOX <<<")
    print("=" * 60)
    print()

    # --- Attente de la validation physique ---
    status = "pending"
    while status == "pending":
        time.sleep(2)
        track = http_json(f"{api}/login/authorize/{track_id}")
        status = track["result"]["status"]
        print(f"  statut : {status}", end="\r")

    print()
    if status != "granted":
        print(f"ERREUR : autorisation non accordée (statut final : {status}).")
        print("Relancez le script et validez sur la box dans le temps imparti.")
        sys.exit(1)

    # --- Sauvegarde du token dans la config, permissions restreintes ---
    cfg["app_token"] = app_token
    data = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
    tmp = args.config + ".tmp"
    try:
        # Écriture atomique via fichier temporaire (cas fichier normal).
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, args.config)
    except OSError:
        # En conteneur, la config est un bind mount de fichier : le répertoire
        # parent n'est pas inscriptible et remplacer un point de montage est
        # interdit. On écrit alors directement dans le fichier (même inode,
        # donc le contenu est bien mis à jour côté hôte).
        try:
            os.remove(tmp)
        except OSError:
            pass
        with open(args.config, "w", encoding="utf-8") as f:
            f.write(data)
    try:
        os.chmod(args.config, 0o600)
    except OSError:
        # Montage depuis Windows (9p/virtiofs) : chmod impossible, non bloquant.
        pass

    print("Autorisation accordée ! app_token enregistré dans la config.")
    print()
    print("DERNIÈRE ÉTAPE IMPORTANTE (moindre privilège) :")
    print("  Freebox OS -> Paramètres -> Gestion des accès -> Applications")
    print(f"  -> '{cfg['app_name']}' : décochez TOUTES les permissions.")
    print("     C'est suffisant : la lecture de la connexion et des sessions")
    print("     VPN fonctionne sans aucune permission (vérifié, API v16.0).")
    print()
    print("Vous pouvez maintenant lancer le moniteur : ./fbxctl.sh start")


if __name__ == "__main__":
    main()
