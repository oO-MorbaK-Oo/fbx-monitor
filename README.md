# Moniteur Freebox → SMS

Surveillance de la Freebox depuis une machine du LAN, via
l'API locale de la box. Alertes SMS via l'API Free Mobile en cas de :

- changement d'adresse IPv4 publique
- bascule de support (fibre ↔ secours 4G/5G) — champ `media`
- rétablissement de la connexion : pas de SMS pour le « down » seul (peu
  utile, et un SMS ne peut de toute façon pas partir sans Internet) ; un
  unique récap au retour `up` avec heure de coupure, heure de retour, durée
  et media du retour (`etat_connexion`)
- connexion / déconnexion d'un utilisateur au serveur VPN (WireGuard)
- reprise après un trou de surveillance : au redémarrage, si l'écart depuis
  le dernier cycle est anormal (≫ `intervalle_s`), un SMS signale la durée de
  l'interruption — couvre le cas d'une **coupure de courant totale** où rien
  n'a pu alerter pendant le blackout (`reprise_apres_coupure`)
- heartbeat périodique optionnel ("la surveillance tourne toujours")

Zéro dépendance : Python 3 standard uniquement. **Le déploiement se fait
exclusivement en conteneur**, avec **Docker ou Podman indifféremment**, sur
système Unix-like (Linux, macOS, *BSD) comme sous Windows, et avec redémarrage
automatique : le moniteur doit tourner en permanence.

## Prérequis

1. **Clé API SMS Free Mobile** : Espace Abonné Free Mobile → Mes options →
   "Notifications par SMS" → activer, noter l'identifiant et la clé.
2. La machine hôte doit être **sur le LAN** de la Freebox (l'API n'est pas
   exposée à l'extérieur, et c'est très bien comme ça) — ou connectée au VPN,
   ce qui revient au même.

## Préparation

1. Copiez `config.example.json` en `config.json` et renseignez `sms.user` /
   `sms.pass`. Laissez **`state_file` à `"/data/state.json"`** (chemin
   interne au conteneur, monté sur le volume `fbx-state`).
2. `chmod 600 config.json`
3. **Permissions selon le moteur** (détail dans « Réglages selon l'OS et le
   moteur ») : en **Docker rootful**, `sudo chown 10001:10001 config.json` (le
   conteneur tourne sous l'UID 10001 ; sans ça, un fichier en 600 lui est
   illisible → `PermissionError`, et éditer la config demandera ensuite
   `sudo`) ; en **Podman rootless**, rien à faire (`--userns=keep-id` s'en
   charge) ; sous **Windows**, sans objet (permissions NTFS larges).

## Enrôlement (une fois par machine qui héberge le moniteur)

L'enrôlement doit écrire l'app_token dans la config : on monte donc la
config **en écriture** pour cette étape uniquement, et on court-circuite
l'entrypoint. Le script s'occupe de tout (build compris, et
`--userns=keep-id` ajouté tout seul si Podman rootless) :

```bash
./fbxctl.sh enroll          # Unix-like (Linux, macOS, *BSD)
.\fbxctl.ps1 enroll         # Windows (PowerShell)
```

Le script construit l'image au passage (pas d'étape de build séparée).

Pendant l'exécution, allez appuyer sur la coche/flèche en façade de la
Freebox pour accorder l'accès. L'`app_token` est alors écrit dans la config.

**Moindre privilège (important)** : ensuite, dans Freebox OS → Paramètres →
Gestion des accès → onglet Applications → votre application : décochez
toutes les permissions. C'est vérifié suffisant : la lecture de l'état de
la connexion et des sessions VPN fonctionne sans aucune permission cochée
(testé sur Freebox Ultra, API v16.0). Si une future version de Freebox OS
resserrait ça, l'erreur `insufficient_rights` apparaîtrait dans les logs.

> Chaque instance du moniteur (une par machine) doit avoir SON app_token :
> refaites l'enrôlement sur chaque machine, avec un `app_id` / `device_name`
> différent dans sa config pour les distinguer dans Freebox OS.

## Lancement

Un script pilote le conteneur quel que soit le moteur (il détecte Docker puis
Podman) et construit l'image au besoin — `fbxctl.sh` sur système Unix-like,
`fbxctl.ps1` sous Windows (PowerShell), avec les **mêmes sous-commandes** :

```bash
./fbxctl.sh enroll     # enrolement one-shot aupres de la box (voir plus haut)
./fbxctl.sh start      # build si besoin + demarrage (equiv. compose up -d --build)
./fbxctl.sh status     # etat + sante + 10 dernieres lignes de log
./fbxctl.sh restart    # relit la config montee
./fbxctl.sh logs       # suivi des logs en continu
./fbxctl.sh stop       # arret volontaire (ne reviendra pas au boot : refaire start)
```

```powershell
# Windows : memes sous-commandes
.\fbxctl.ps1 start
.\fbxctl.ps1 status
```

Sous le capot, `start` s'appuie sur `compose.yaml`, qui applique le
durcissement (rootfs read-only, aucune capability, no-new-privileges) et
`restart: unless-stopped` : le conteneur redémarre tout seul après un crash ou
un reboot de la machine. (Les commandes `docker`/`podman` sous-jacentes sont
lisibles directement dans `fbxctl.sh` si besoin.)

> **Pour aller plus loin (Podman + systemd).** Au lieu de `fbxctl`, vous pouvez
> utiliser le **quadlet** fourni (`fbx-monitor.container`) : copiez-le dans
> `~/.config/containers/systemd/` (rootless) ou `/etc/containers/systemd/`
> (rootful), puis `systemctl --user daemon-reload && systemctl --user start
> fbx-monitor`. Podman génère l'unité systemd tout seul, `Restart=always`, au
> boot — pas de démon central comme Docker.

## Réglages selon l'OS et le moteur

Le comportement du moniteur est identique partout ; seuls quelques points
d'intégration (permissions, chemins, démarrage automatique) changent selon
l'OS et le moteur.

### Systèmes Unix-like (Linux, macOS, *BSD)

- **Docker (rootful)** : le conteneur tourne sous `fbxmon` (UID 10001) et le
  bind mount préserve les UID de l'hôte → `sudo chown 10001:10001 config.json`
  (en gardant le `chmod 600`), sinon `PermissionError` à la lecture.
  `fbxctl.sh` le vérifie avant de lancer.
- **Podman (rootless)** : pas de chown. L'utilisateur `fbxmon` du conteneur
  est mappé sur un sous-UID de votre compte qui ne peut pas lire un fichier en
  600 vous appartenant → ajoutez `--userns=keep-id` (ou décommentez
  `UserNS=keep-id` dans le quadlet) : l'utilisateur du conteneur devient votre
  propre UID. `fbxctl.sh enroll` l'ajoute tout seul.
- **SELinux** (Fedora, RHEL, Alma...) : suffixez les montages avec `:Z`
  (`.../config.json:/config/config.json:ro,Z`), sinon accès refusé.
- **Survie à la déconnexion (Podman rootless)** : `loginctl enable-linger
  $USER`, sinon vos conteneurs s'arrêtent quand votre session se ferme.
  Indispensable pour une surveillance permanente.

### Windows (Docker Desktop ou Podman Desktop)

Sur Windows, Docker comme Podman font tourner les conteneurs dans une VM Linux
(WSL2 / « podman machine ») ; plusieurs points ci-dessus ne s'appliquent pas :

- **Syntaxe des chemins** : `$(pwd)` est du bash. Utilisez `%cd%` (cmd) ou
  `$PWD` (PowerShell) : `-v "%cd%\config.json:/config/config.json:ro"`.
- **chmod 600 impossible** (NTFS) : le moniteur logge un warning « mode 777 »
  à chaque démarrage — non bloquant, la protection du fichier repose sur votre
  session Windows.
- **chown / `--userns=keep-id` sans objet** : les fichiers montés depuis
  Windows arrivent dans la VM avec des permissions larges, le conteneur les
  lit sans mapping ni chown particulier.
- **Après un reboot de Windows**, la VM (WSL2 / podman machine) ne redémarre
  pas forcément seule — et sans elle, plus de surveillance, silencieusement.
  Activez le démarrage automatique (Docker Desktop : « Start Docker Desktop
  when you log in » ; Podman Desktop : autostart de la machine, ou
  `podman machine start` à l'ouverture de session).

## Vérifications

- `docker ps` / `podman ps` : la colonne STATUS doit afficher `healthy`
  après ~1 min — le healthcheck vérifie que `state.json` est rafraîchi.
- **Premier lancement** : le script mémorise l'état initial sans alerter
  (sauf le SMS "surveillance demarree" si activé). Les alertes ne partent
  qu'aux changements suivants.
- **Test grandeur nature du VPN** : connectez votre Mac en 4G via WireGuard →
  vous devez recevoir "VPN: connexion de NBC_MacBook depuis x.x.x.x" dans
  la minute (selon `intervalle_s`).
- **Valeur du champ `media`** : en fibre, la box répond `ftth` (observé sur
  Freebox Ultra, API v16.0). Le libellé côté secours 4G/5G reste inconnu ;
  le jour d'une bascule le SMS vous le donnera. L'alerte fonctionne quel que
  soit le libellé, puisqu'elle se déclenche sur *tout* changement.
- Les logs vont sur stdout : `docker logs` / `podman logs` /
  `journalctl --user -u fbx-monitor` (quadlet).

## Dépannage

- **`PermissionError: ... '/data/state.json.tmp'` dans les logs** : le volume
  d'état n'appartient pas à l'utilisateur `fbxmon` (UID 10001) du conteneur.
  Les images récentes règlent ça toutes seules (le `/data` de l'image est
  attribué à `fbxmon`, dont hérite un volume neuf). Mais un volume **créé
  avec une ancienne image** reste en `root` et un simple rebuild ne le
  corrige pas : il faut le recréer.

  ```bash
  ./fbxctl.sh stop
  docker volume rm fbx-monitor_fbx-state   # nom = <projet>_fbx-state ; docker volume ls pour le retrouver
  ./fbxctl.sh start                        # rebuild + volume recree, proprement attribue
  ```

  (Podman : `podman volume rm ...` ; le préfixe de projet peut différer.)
  Le volume ne contient que le `state.json` de référence, reconstruit au
  démarrage suivant — sa suppression est sans risque (juste un nouveau SMS
  "surveillance demarree").

## Notes

- Les SMS sont plafonnés (`max_sms_par_cycle`) et espacés
  (`delai_entre_sms_s`) pour respecter l'API Free Mobile.
- Si la box est injoignable (reboot), le script logge et réessaie au cycle
  suivant sans spammer de SMS ; un éventuel changement d'IP sera détecté et
  notifié dès son retour.
- WireGuard n'ayant pas de "déconnexion" explicite, une session peut rester
  listée un moment après la coupure réelle du client : les SMS de
  déconnexion peuvent donc arriver avec quelques minutes de retard.
- L'état persistant vit dans un volume nommé (`/data/state.json`). Le supprimer
  = repartir d'une base neuve (pas d'alertes au cycle suivant, juste un SMS de
  démarrage). Son nom réel est préfixé par le projet (ex.
  `fbx-monitor_fbx-state`) ; `docker volume ls` / `podman volume ls` le donne.
- Le conteneur n'expose **aucun port** (trafic sortant uniquement, vers la
  box et l'API SMS) : le réseau bridge par défaut suffit, rien à ouvrir.
- **Coupure de courant** : pendant un blackout total (box + machine éteintes),
  aucune alerte n'est possible — plus rien n'a d'électricité ni d'Internet. Le
  moniteur envoie un SMS *a posteriori* au retour (« surveillance reprise après
  interruption d'environ X »), donnant la durée du trou mais pas l'instant
  exact du retour. Pour une alerte *pendant* la coupure, il faudrait un
  onduleur (UPS) alimentant box + machine, permettant d'envoyer le SMS via la
  4G avant extinction — non implémenté (piste future).
