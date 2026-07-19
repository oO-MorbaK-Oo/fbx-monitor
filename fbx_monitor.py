#!/usr/bin/env python3
"""
Moniteur Freebox : surveille via l'API locale de la box
  - l'adresse IPv4 publique (alerte si changement)
  - le support de connexion / media (alerte si bascule fibre <-> secours 4G/5G)
  - l'état de la connexion (up / down / ...)
  - les connexions au serveur VPN (alerte connexion / déconnexion)
et envoie les alertes par SMS via l'API Free Mobile.

Usage :
    python3 fbx_monitor.py --config /etc/fbx-monitor/config.json          # boucle infinie
    python3 fbx_monitor.py --config /etc/fbx-monitor/config.json --once   # un seul cycle (cron)

Aucune dépendance externe : uniquement la bibliothèque standard.
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("fbx-monitor")

# ---------------------------------------------------------------------------
# Client API Freebox
# ---------------------------------------------------------------------------


class FreeboxError(Exception):
    pass


class AuthRequired(FreeboxError):
    pass


class FreeboxClient:
    def __init__(self, base_url, app_id, app_token, timeout=8):
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self.app_token = app_token
        self.timeout = timeout
        self.api = None            # ex: http://mafreebox.freebox.fr/api/v12
        self.session_token = None

    # --- bas niveau -------------------------------------------------------

    def _raw(self, url, payload=None, with_session=True):
        data = None
        headers = {"Content-Type": "application/json"}
        if with_session and self.session_token:
            headers["X-Fbx-App-Auth"] = self.session_token
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # La Freebox renvoie 403 avec un corps JSON en cas de session expirée
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                raise FreeboxError(f"HTTP {e.code} sur {url}") from e
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            raise FreeboxError(f"Freebox injoignable ({url}) : {e}") from e

        if body.get("success") is False:
            if body.get("error_code") in ("auth_required", "invalid_token",
                                          "invalid_session"):
                raise AuthRequired(body.get("msg", "session invalide"))
            raise FreeboxError(f"Erreur API : {body.get('error_code')} "
                               f"- {body.get('msg')}")
        return body

    # --- session ------------------------------------------------------------

    def _discover(self):
        body = self._raw(self.base_url + "/api_version", with_session=False)
        major = body["api_version"].split(".")[0]
        self.api = f"{self.base_url}{body['api_base_url']}v{major}"
        log.info("API Freebox détectée : v%s (%s)",
                 body["api_version"], body.get("box_model", "?"))

    def login(self):
        if self.api is None:
            self._discover()
        chal = self._raw(self.api + "/login/", with_session=False)
        challenge = chal["result"]["challenge"]
        password = hmac.new(self.app_token.encode("utf-8"),
                            challenge.encode("utf-8"),
                            hashlib.sha1).hexdigest()
        sess = self._raw(self.api + "/login/session/",
                         {"app_id": self.app_id, "password": password},
                         with_session=False)
        self.session_token = sess["result"]["session_token"]
        log.info("Session Freebox ouverte.")

    def get(self, path):
        """GET authentifié avec ré-ouverture de session automatique."""
        if self.session_token is None:
            self.login()
        # L'API omet la clé "result" quand le résultat est vide (ex. aucune
        # session VPN active) : .get() et non [], sinon KeyError.
        try:
            return self._raw(self.api + path).get("result")
        except AuthRequired:
            log.info("Session expirée, ré-authentification...")
            self.login()
            return self._raw(self.api + path).get("result")

    # --- endpoints métier ---------------------------------------------------

    def connection_status(self):
        """État de la connexion Internet : ipv4, media, state, ..."""
        return self.get("/connection/") or {}

    def vpn_connections(self):
        """Liste des connexions actives au serveur VPN (peut être None)."""
        return self.get("/vpn/connection/") or []


# ---------------------------------------------------------------------------
# Envoi de SMS (API Free Mobile)
# ---------------------------------------------------------------------------


class SmsSender:
    URL = "https://smsapi.free-mobile.fr/sendmsg"

    def __init__(self, cfg):
        self.user = cfg["user"]
        self.password = cfg["pass"]
        self.delay = cfg.get("delai_entre_sms_s", 3)
        self.max_per_cycle = cfg.get("max_sms_par_cycle", 5)
        self._sent_this_cycle = 0

    def new_cycle(self):
        self._sent_this_cycle = 0

    def send(self, message):
        prefix = time.strftime("[%d/%m %H:%M]")
        full = f"{prefix} {message}"
        if self._sent_this_cycle >= self.max_per_cycle:
            log.warning("Quota SMS du cycle atteint, non envoyé : %s", full)
            return False
        if self._sent_this_cycle > 0:
            time.sleep(self.delay)
        params = urllib.parse.urlencode(
            {"user": self.user, "pass": self.password, "msg": full})
        try:
            with urllib.request.urlopen(f"{self.URL}?{params}", timeout=15) as r:
                ok = (r.status == 200)
        except urllib.error.HTTPError as e:
            log.error("API SMS : HTTP %s (%s)", e.code, full)
            return False
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            log.error("API SMS injoignable : %s", e)
            return False
        if ok:
            self._sent_this_cycle += 1
            log.info("SMS envoyé : %s", full)
        return ok


# ---------------------------------------------------------------------------
# État persistant
# ---------------------------------------------------------------------------


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def vpn_key(conn):
    """Identifiant stable d'une session VPN (défensif selon versions d'API)."""
    if conn.get("id") is not None:
        return str(conn["id"])
    return f"{conn.get('user', '?')}@{conn.get('src_ip', '?')}"


def vpn_label(conn):
    user = conn.get("user", "?")
    src = conn.get("src_ip", "?")
    server = conn.get("vpn", "")
    extra = f" ({server})" if server else ""
    return f"{user} depuis {src}{extra}"


def _hhmm(ts):
    """Heure locale HH:MM d'un timestamp epoch."""
    return time.strftime("%H:%M", time.localtime(ts))


def _duree(secs):
    """Durée lisible et sans accent (ex. '2h05', '7min30', '12s')."""
    secs = int(secs)
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h{m:02d}"
    if m:
        return f"{m}min{s:02d}"
    return f"{s}s"


# ---------------------------------------------------------------------------
# Un cycle de surveillance
# ---------------------------------------------------------------------------


def run_cycle(fbx, sms, notify, state):
    """Compare l'état courant à l'état précédent, alerte, retourne le nouvel état."""
    sms.new_cycle()
    now = time.time()
    first_run = state is None
    if first_run:
        state = {}

    new_state = dict(state)

    # ---- Connexion Internet ----
    try:
        conn = fbx.connection_status()
    except FreeboxError as e:
        # Box injoignable : on log mais on n'alerte pas par SMS (on ne peut
        # de toute façon pas savoir si c'est la box ou nous ; et si la box
        # reboote, l'alerte "changement d'IP" partira au cycle suivant).
        log.error("Lecture état connexion impossible : %s", e)
        conn = None

    if conn is not None:
        ipv4 = conn.get("ipv4")
        media = conn.get("media")
        cstate = conn.get("state")

        if not first_run:
            if notify.get("changement_ip") and ipv4 and ipv4 != state.get("ipv4"):
                sms.send(f"Freebox: IP publique changee: "
                         f"{state.get('ipv4', '?')} -> {ipv4}")
            if notify.get("changement_media") and media and media != state.get("media"):
                sms.send(f"Freebox: bascule de support: "
                         f"{state.get('media', '?')} -> {media} "
                         f"(fibre vs secours 4G/5G ?)")
            # Etat de la connexion : un "down" seul est peu utile (et de toute
            # facon un SMS ne peut pas sortir sans Internet). On memorise juste
            # l'heure de la coupure, sans SMS, et on envoie UN recapitulatif au
            # retour "up" : heure de coupure, heure de retour, duree, media.
            if cstate:
                was_up = state.get("state") == "up"
                is_up = cstate == "up"
                if was_up and not is_up:
                    new_state["down_since"] = now
                    log.info("Connexion perdue (state=%s) a %s ; "
                             "SMS au retablissement", cstate, _hhmm(now))
                elif is_up and not was_up:
                    down_since = state.get("down_since")
                    if notify.get("etat_connexion"):
                        msg = f"Freebox: connexion RETABLIE a {_hhmm(now)}"
                        if down_since:
                            msg += (f" (perdue a {_hhmm(down_since)}, "
                                    f"coupure {_duree(now - down_since)})")
                        msg += f", media {media or '?'}"
                        sms.send(msg)
                    new_state.pop("down_since", None)
        else:
            log.info("Etat initial : ipv4=%s media=%s state=%s",
                     ipv4, media, cstate)
            # Si on demarre alors que la connexion est deja coupee, on horodate
            # pour pouvoir donner la duree au retablissement.
            if cstate and cstate != "up":
                new_state["down_since"] = now

        # Pendant une coupure, ipv4/media peuvent revenir vides : on garde la
        # derniere valeur connue pour que le SMS de retour compare a du concret
        # (ex. "A -> B", "ftth -> 4g" plutot que " -> B").
        if ipv4:
            new_state["ipv4"] = ipv4
        if media:
            new_state["media"] = media
        new_state["state"] = cstate

    # ---- Connexions VPN ----
    try:
        vpn = fbx.vpn_connections()
    except FreeboxError as e:
        log.error("Lecture connexions VPN impossible : %s", e)
        vpn = None

    if vpn is not None:
        current = {vpn_key(c): vpn_label(c) for c in vpn}
        previous = state.get("vpn", {}) if not first_run else current

        if not first_run:
            for key, label in current.items():
                if key not in previous and notify.get("vpn_connexion"):
                    sms.send(f"VPN: connexion de {label}")
            for key, label in previous.items():
                if key not in current and notify.get("vpn_deconnexion"):
                    sms.send(f"VPN: deconnexion de {label}")
        else:
            for label in current.values():
                log.info("Session VPN deja active au demarrage : %s", label)

        new_state["vpn"] = current

    # Horodatage du dernier cycle : sert a reperer un "trou" de surveillance
    # au redemarrage (coupure de courant, reboot, arret prolonge).
    new_state["last_cycle"] = now

    return new_state


def maybe_heartbeat(sms, state, every_days):
    """SMS périodique 'la surveillance tourne toujours'."""
    if not every_days:
        return
    now = time.time()
    last = state.get("last_heartbeat", 0)
    if now - last >= every_days * 86400:
        if sms.send(f"Moniteur Freebox: toujours actif "
                    f"(IP {state.get('ipv4', '?')}, media {state.get('media', '?')})"):
            state["last_heartbeat"] = now


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Moniteur Freebox -> SMS")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--once", action="store_true",
                        help="Exécute un seul cycle puis quitte (usage cron)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    if not cfg.get("app_token"):
        log.error("app_token absent de la config : lancez d'abord fbx_enroll.py")
        sys.exit(1)

    # Avertissement si les secrets sont lisibles par d'autres
    mode = os.stat(args.config).st_mode & 0o777
    if mode & 0o077:
        log.warning("Le fichier de config %s est lisible par d'autres "
                    "utilisateurs (mode %o) : faites un chmod 600 !",
                    args.config, mode)

    fbx = FreeboxClient(cfg["freebox_url"], cfg["app_id"], cfg["app_token"])
    sms = SmsSender(cfg["sms"])
    notify = cfg.get("notifier", {})
    state_file = cfg.get("state_file", "/var/lib/fbx-monitor/state.json")
    interval = max(10, int(cfg.get("intervalle_s", 60)))
    heartbeat_days = cfg.get("heartbeat_jours", 0)

    state = load_state(state_file)

    if state is None and notify.get("demarrage_script"):
        sms.new_cycle()
        sms.send("Moniteur Freebox: surveillance demarree")
    elif state and notify.get("reprise_apres_coupure"):
        # Trou de surveillance : ecart anormal depuis le dernier cycle ecrit.
        # Couvre le cas "coupure de courant totale" (box + machine eteintes) :
        # rien n'a pu alerter pendant le blackout, on previent au retour.
        last = state.get("last_cycle")
        now = time.time()
        if last and now - last > interval * 3:
            sms.new_cycle()
            sms.send(f"Moniteur Freebox: surveillance reprise apres "
                     f"interruption d'environ {_duree(now - last)} "
                     f"(jusqu'a {_hhmm(now)}). Coupure de courant, "
                     f"reboot ou arret ?")

    # Arrêt propre sous systemd
    stop = {"flag": False}

    def _sigterm(signum, frame):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    while True:
        try:
            state = run_cycle(fbx, sms, notify, state)
            maybe_heartbeat(sms, state, heartbeat_days)
            save_state(state_file, state)
        except Exception:
            # Un bug ne doit jamais tuer la surveillance ; systemd
            # redémarrerait le service, mais autant survivre nous-mêmes.
            log.exception("Erreur inattendue pendant le cycle")

        if args.once or stop["flag"]:
            break
        # Sommeil interruptible par SIGTERM
        for _ in range(interval):
            if stop["flag"]:
                break
            time.sleep(1)
        if stop["flag"]:
            break

    log.info("Arrêt du moniteur.")


if __name__ == "__main__":
    main()
