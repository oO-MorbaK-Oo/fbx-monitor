# Moniteur Freebox -> SMS
# Image multi-arch (amd64 / arm64) : buildez simplement sur chaque machine,
# ou cross-buildez avec buildx si besoin.
#
# Build :  docker build -t fbx-monitor .        (idem avec podman)

FROM python:3.12-alpine

# tzdata pour que l'horodatage des SMS soit en heure locale (TZ ci-dessous)
RUN apk add --no-cache tzdata \
 && adduser -D -H -u 10001 fbxmon

ENV TZ=Europe/Paris \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY fbx_monitor.py fbx_enroll.py ./

# Le conteneur attend :
#   /config/config.json  (monté depuis l'hôte, :ro en fonctionnement normal)
#   /data                (volume pour state.json ; config: "state_file": "/data/state.json")
# /data doit appartenir a fbxmon AVANT le VOLUME : un volume nomme vide herite
# a l'initialisation des droits du repertoire de l'image (sinon root:root =>
# PermissionError sur state.json.tmp pour l'UID 10001).
RUN mkdir -p /data && chown fbxmon:fbxmon /data
VOLUME /data

USER fbxmon

# Sain si state.json a été rafraîchi il y a moins de 5 min (intervalle 60s * marge)
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python3 -c "import os,sys,time; st=os.stat('/data/state.json'); sys.exit(0 if time.time()-st.st_mtime < 300 else 1)"

ENTRYPOINT ["python3", "/app/fbx_monitor.py", "--config", "/config/config.json"]
