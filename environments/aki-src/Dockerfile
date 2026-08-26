FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git patch \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt
COPY aki-source/ /opt/aki/
COPY proteus-worker/ /opt/proteus/
COPY controller.patch /tmp/controller.patch
WORKDIR /opt/aki
RUN patch -p1 < /tmp/controller.patch \
 && python -m pip install --no-cache-dir --no-deps /opt/aki \
 && rm -rf /opt/aki/.git /opt/aki/.env /tmp/controller.patch /tmp/requirements.txt \
 && tar --exclude=aki/.venv --exclude=aki/.git --exclude=aki/.env \
        -cf /opt/aki-source.tar -C /opt aki \
 && find /opt/aki -path /opt/aki/.venv -prune -o -type f -print \
        | sort > /opt/source-manifest.txt
COPY boot.sh /usr/local/bin/aki-proteus-boot
RUN chmod +x /usr/local/bin/aki-proteus-boot \
 && chmod -R a+rX /opt/aki /opt/proteus
WORKDIR /workspace
ENTRYPOINT ["aki-proteus-boot"]
