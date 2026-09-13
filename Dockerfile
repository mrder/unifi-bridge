FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persists the authkey issued at adoption (and the web UI's saved setup) so a
# container restart doesn't force re-adopting the device or re-doing setup.
VOLUME ["/data"]

EXPOSE 8099

# webui.py serves the setup/status page AND starts the bridge itself (in a
# background thread) once configured -- either via the form or, for
# no-web-UI/pure-env-var operation, run `bridge_daemon.py` directly instead
# (docker run ... python3 bridge_daemon.py).
ENTRYPOINT ["python3", "webui.py"]
