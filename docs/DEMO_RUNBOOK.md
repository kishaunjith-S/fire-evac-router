# Demo Runbook — Dynamic Fire Evacuation Router

Read this top to bottom **before judges arrive**. Total setup time: ~5 minutes
if everything's clean, longer if you hit a known issue below.

---

## 1. Startup sequence (exact order matters)

You need **4 things running simultaneously** in separate terminals/windows.
Start them in this order — each one depends on the previous being alive.

### Terminal 1 — Mosquitto broker
Usually already running as a Windows service in the background. Verify:
```powershell
Get-Service -Name "mosquitto"
```
Should show `Status: Running`. If not running, start it, or run manually:
```powershell
cd "C:\Program Files\mosquitto"
.\mosquitto.exe -v
```
**Don't** run this manually if the service is already up — you'll get a
"port already in use" error (harmless, but confusing under pressure).

### Terminal 2 — Coordinator + all 36 zone nodes
```powershell
cd "Z:\Honeywell - Fire Evacuation\fire-evac-router"
python node/run_all_nodes.py
```
Wait for it to report all 36 zones started and connected. Leave this
window visible — it's your live diagnostic feed if anything looks wrong
during the demo.

### Terminal 3 — Injector (Flask web app)
```powershell
cd "Z:\Honeywell - Fire Evacuation\fire-evac-router"
python injector/app.py
```
Wait for `Running on http://127.0.0.1:5000` (or whatever port it prints).

### Terminal 4 — Node-RED
```powershell
node-red
```
Wait for `Server now running at http://127.0.0.1:1880/`.

---

## 2. Verification checklist (do this BEFORE judges arrive, not during)

Open two browser tabs:
- **Injector**: `http://127.0.0.1:5000`
- **Node-RED Dashboard**: `http://127.0.0.1:1880/ui`

Check, in order:

- [ ] Injector shows `LINK: Connected`
- [ ] Injector shows `NODES ONLINE: 36 / 36`
- [ ] Node-RED dashboard also shows `NODES ONLINE: 36 / 36`
- [ ] All grid cells are green (idle/safe state) on both views
- [ ] Click any zone → **Trigger Flashover** → that cell turns red with
      the blocked/shelter icon, neighboring cells show arrows rerouting
      around it, on **both** the injector and Node-RED dashboard
- [ ] Alarm tone plays and "ALARM ACTIVE" indicator appears when
      triggered
- [ ] Click **Clear** → cell returns to green, arrows re-normalize,
      alarm indicator disappears
- [ ] `LAST RECOMPUTE LATENCY` shows a reasonable number (should be well
      under 300ms in the typical case; occasional short-lived spikes
      under ~1s are expected and not a failure — see Known Issues)

If all boxes check, you're demo-ready. **Do a hard refresh
(`Ctrl+Shift+R`) on both browser tabs right before judges arrive** — this
clears any stale state from your own testing and gives a clean first
impression (`0/36` briefly climbing to `36/36` as it connects is actually
a nice visual proof the system is live, not fake).

---

## 3. Suggested live demo script (~3-4 minutes)

Judges will select a zone themselves per the problem statement's "Live
Test Case," so don't over-script — but have this flow ready to guide it:

1. **Open with the injector** — point out the 6x6 grid, two exits, all
   green/idle. Mention this represents a multi-story commercial building
   floor, simulated rather than physical hardware.
2. **Let the judge pick a zone** (or pick one yourself if they don't) →
   click Trigger Flashover.
3. **Narrate what's happening in real time**: "The zone just received a
   simulated high-heat, high-smoke reading. Our fusion formula combines
   temperature, smoke density, and flame presence — smoke has a
   non-linear exponential weight because visibility degrades much faster
   than temperature rises. That pushed the edge cost above our
   shelter-in-place threshold, so instead of giving a false 'safe' route
   through a blocked corridor, the system marks it correctly."
4. **Point to the reroute**: "Every neighboring zone's LED direction just
   recalculated — you can see the arrows now point around the hazard
   toward the nearest exit. That recompute happened in [X]ms, well under
   our 300ms budget."
5. **Switch to the Node-RED dashboard tab** — same data, this is the
   "Fire Commander" central monitoring view a building safety officer
   would actually watch, separate from the injector which is just our
   test harness.
6. **If asked about hardware**: "We built this fully simulated by design
   — the architecture is hardware-agnostic. Swapping the injector for
   real DHT22/MQ-2/IR sensors on an ESP32 requires no change to the
   coordinator or routing logic, since sensor input and physical LED
   output are both isolated behind the same MQTT topic contracts."
7. **If asked about robustness/fail-safe**: mention the two real bugs
   found and fixed during load testing (see
   `docs/latency_debugging_notes.md`) — this is a strong, specific answer
   that shows the system was actually stress-tested, not just built and
   demoed once.

---

## 4. Known issues / troubleshooting

**Symptom: latency climbing steadily over several minutes of runtime**
This happened during development after long testing sessions (not on
fresh restarts). If you see this before judging, do a clean restart of
Terminal 2 and 3 (Ctrl+C both, rerun). Root-caused and fixed for the
common case; a long-lived process may still show mild drift — restarting
right before demo avoids this entirely. See `docs/latency_debugging_notes.md`
for the full root-cause writeup if asked.

**Symptom: `NODES ONLINE` stuck at 0 in Node-RED dashboard**
Check the health MQTT-in node's Topic field is exactly
`fire/system/health/+` (must include the trailing `+` wildcard). Missing
the `+` was the actual cause the one time this happened during
development.

**Symptom: dashboard/injector shows stale grid state (arrows/colors from
a previous test)**
Hard refresh (`Ctrl+Shift+R`) — both pages cache aggressively.

**Symptom: `mosquitto : term not recognized`**
You're not in the Mosquitto install directory, or trying to run it when
the Windows service already owns port 1883. Check
`Get-Service -Name "mosquitto"` first before running manually.

**Symptom: Flask injector page won't load, `ERR_CONNECTION_REFUSED`**
Terminal 3 (`injector/app.py`) isn't running or crashed on startup —
check that terminal for a Python traceback.

**Symptom: Node-RED dashboard white/blue chrome looks inconsistent with
the dark grid panel**
Cosmetic only, known and accepted — does not affect functionality or
scoring-relevant behavior. Don't spend demo time apologizing for it.

---

## 5. Shutdown (after demo, or end of day)

Ctrl+C in Terminals 2, 3, 4 (in any order). Mosquitto service can keep
running in the background — no need to stop it.