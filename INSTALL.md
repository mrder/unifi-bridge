# Installation guide (Unraid / Docker)

This walks through getting the bridge running against a real switch, start
to finish. See `README.md` for *why* things work the way they do; this
document is just the *steps*.

## 0. Before you start

- **Your switch must be at factory defaults with its default admin login.**
  The bridge takes over the switch's identity from first contact. If it's
  already configured, either factory-reset it or be ready to type its
  current IP/username/password into the setup form instead of relying on
  the built-in defaults.
- **Only one admin session at a time, per switch.** Confirmed live on both
  supported switches: logging into the switch's web UI from a second place
  (your own browser, a script, anything) while the bridge is running will
  silently kick its session. The bridge recovers from this automatically
  for routine operation, but avoid poking around the switch's own web UI
  while adopting it for the first time, to keep things simple.
- **Network reachability, both directions:**
  - The container needs to reach your **UniFi controller** (normally your
    main LAN -- a macvlan network like the examples below).
  - The container needs to reach the **switch's management IP**. If the
    switch is still at its factory IP (often a different subnet than your
    main LAN -- e.g. the D-Link profile's default is `10.90.90.90`, a
    `10.0.0.0/8` address), see `README.md`'s "Network prerequisite" section
    for how to handle that *before* starting the container. If the switch
    is already on your main LAN (as a NETGEAR "Smart Managed Plus" switch
    typically would be once configured), this just works.

## 1. Get the code onto your Unraid box

```bash
git clone https://github.com/mrder/unifi-bridge.git
cd unifi-bridge
cp .env.example .env
```

You don't need to edit `.env` -- leave everything in it commented out if
you'd rather configure the switch/controller through the web UI in step 3.
Compose just needs the file to exist.

## 2. Build and start the container

```bash
docker compose up -d --build
```

Check it started cleanly:

```bash
docker compose logs -f
```

You should see Flask's startup banner and no tracebacks. Ctrl+C to stop
following logs (the container keeps running).

## 3. Open the setup web UI

Because this runs on a macvlan network, the container gets its own real LAN
IP -- there's no `localhost:PORT` to open. Find the container's address:

```bash
docker inspect unifi-bridge | grep -A5 '"br0"'
```

(or `docker network inspect br0` and find `unifi-bridge` in the list).
Open `http://<that-ip>:8099` in a browser.

On first load you'll see a setup form:

1. **Switch type** -- pick yours from the dropdown.
2. **Switch IP** -- click "Scan for it" to try to find it automatically
   (tries the type's factory-default IP first, then sweeps the container's
   own local network as a fallback -- see README for exactly how), or type
   it in directly if you already know it.
3. **Switch username/password** -- leave blank to use the profile's
   defaults, or fill in if the switch isn't at factory settings.
4. **Controller host/port** -- your UniFi controller's address (port 8080
   unless you've changed it).

Click **Save & Start**. The page switches to a status view with a live log.

## 4. Watch it find your switch

Within a few seconds the log should show something like:

```
connected to real switch: <model> fw=<version> mac=<mac address>
no saved adoption -- waiting for 'Adopt' click in the controller UI
not adopted yet (HTTP 404) -- waiting for 'Adopt' click in the UI, ...
```

That `404` every ~10 seconds is normal and expected -- it means the bridge
is broadcasting discovery and announcing itself, and is just waiting for a
human to approve it. It is **not** an error.

## 5. Adopt it

Open your UniFi controller's **Devices** page. Within a few discovery
cycles, your switch should appear as **Pending Adoption** -- showing up as
whichever real UniFi model the bridge impersonates for your switch type
(see `switch_profiles.py`; this is a real, deliberate stand-in, not a bug --
README explains why). Click **Adopt**.

The log should immediately show:

```
adoption command received -- device has been adopted in the UI
adopted successfully, authkey saved to /data/bridge_state.json
applied setparam (cfgversion=...)
```

...followed by a steady stream of `received response type=noop: {...}`
roughly every 30 seconds. That's the healthy steady state: the controller
has nothing new to tell the bridge, and the bridge has nothing new to
report. In the UniFi UI the device should now show **Connected**, with its
real port list and link status.

## 6. Confirm it survives a restart

```bash
docker compose restart unifi-bridge
docker compose logs -f
```

You should see `resuming with previously saved authkey from
/data/bridge_state.json` and the device go straight back to reporting --
no re-adoption needed. The authkey and setup are both persisted in the
`./data` folder next to `docker-compose.yml` (bind-mounted into the
container), so this survives container rebuilds too, not just restarts.

## Troubleshooting

- **Nothing shows up as Pending after a few minutes.** Check the container
  log for the actual HTTP status on each discovery attempt. A `404` is
  normal (not adopted yet); anything else, or a connection error, points at
  a network reachability problem between the container and the controller
  -- see the network prerequisite notes in `README.md` and `docker-
  compose.yml`.
- **Switch login fails** (`Could not log into switch at <ip>`). Either the
  switch isn't at the IP/credentials you gave it, or something else is
  mid-session with it right now (see the single-session note above) -- wait
  a few seconds and let the bridge retry, or restart the container.
- **The device drops from Connected back to offline.** Check the log for
  repeated inform failures. If it's a network blip, the bridge will keep
  retrying and recover on its own. If it's a crash, please open a GitHub
  issue with the log output around the failure.
- **Want to reconfigure (different switch/controller)?** The status page
  has a "Reconfigure" link. It only clears the web UI's saved setup, not
  the switch's adoption state (`bridge_state.json`) -- so if you're
  pointing at a *different* switch, also stop the container and delete
  `./data/bridge_state.json` first, or the bridge will try to resume an
  adoption that doesn't match the new switch.

## Prefer no web UI at all?

Run the bridge directly with env vars instead of the default web UI
entrypoint -- see `.env.example` for every setting, and swap the
`Dockerfile`'s `ENTRYPOINT` (or override the command in
`docker-compose.yml`) to `python3 bridge_daemon.py`. Both paths read/write
the same `/data/bridge_state.json`, so you can switch between them freely.
