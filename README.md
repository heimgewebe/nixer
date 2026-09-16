# Nixer

Nixer ist der eng begrenzte Nix-Spezialist im Heimgewebe-Operator-Ökosystem.

> Er macht nichts, er kann nichts, außer Nix. Und das sehr gut.

Der Dienst ist bewusst **kein** allgemeiner Operator. Er analysiert Nix, Nixpkgs und NixOS mit realer Evaluation, besitzt aber keine Repo-, PR-, Bureau-, Deploy- oder Service-Autorität.

## v0

Nixer v0 bietet einen kleinen, semantischen MCP-Werkzeugkatalog:

- `nixer_status` — Scope, Verbote und Backend-Bereitschaft;
- `flake_metadata` — reale Flake-Metadaten;
- `flake_show` — reale Outputstruktur eines Flakes;
- `flake_check` — Evaluation per `nix flake check --no-build`;
- `eval_attr` — einen streng validierten Flake-Attributpfad evaluieren;
- `nixos_option` — einen resultierenden NixOS-Optionswert evaluieren;
- `derivation_show` — resultierende Derivation anzeigen;
- `build_dry_run` — Buildplan prüfen, ohne den Output zu realisieren.

Es gibt absichtlich **kein** `run_shell_command` und keinen frei zusammensetzbaren Nix-ARGV-Endpunkt.

## Ausführungsmodell

Der Heim-PC hat aktuell kein Host-Nix im normalen Ausführungspfad. Nixer verwendet daher denselben grundsätzlichen Trust-Ansatz wie der bestehende NixOS-Produktionspfad des `heim-pc`:

- Nix läuft in Docker;
- das Image ist an eine unveränderliche Image-ID gebunden;
- das untersuchte Repository wird read-only unter `/workspace` eingehängt;
- ein fest verdrahteter Bootstrap klont den exakten Git-HEAD in den flüchtigen, root-eigenen Snapshot `/tmp/nixer-workspace` und überlagert ausschließlich Änderungen an bereits getrackten Dateien; ungetrackte und ignorierte Dateien werden nie Teil der Nix-Flake-Quelle;
- Nix evaluiert `git+file:///tmp/nixer-workspace`; saubere Checkouts behalten damit ihre echte Git-Revision, während getrackte lokale Änderungen als dirty Git-Quelle sichtbar bleiben;
- der Container startet mit `--cap-drop=ALL` und erhält nur `CHOWN` sowie `DAC_READ_SEARCH`, die für den Single-User-Nix-Store beziehungsweise das read-only Lesen des Host-Checkouts über die UID-Grenze nötig sind;
- Bash, Git und Nix werden ausschließlich über feste absolute Pfade aufgerufen; es gibt keinen frei steuerbaren Shell-Endpunkt;
- keine Docker-Socket- oder sonstigen Hostmounts;
- keine echten Builds oder Aktivierungen in v0.

Das aktuell gebundene Image ist:

```text
sha256:98edc6813218e179ce84587373e0b52d4aa58babae2d26b51fb01e7fdacf815f
```

Es entspricht dem im `heim-pc`-Produktionsvertrag verwendeten `nixos/nix:2.35.2`-Image. Nixer zieht dieses Image nicht selbst. Fehlt es lokal oder stimmt seine Identität nicht exakt, verweigert der Backend-Pfad die Ausführung. Der Host hält dieselbe verifizierte Image-ID zusätzlich unter dem lokalen Tag `nixos/nix:2.35.2`, damit ein gewöhnliches Pruning ungetaggter Images das Backend nicht erneut entfernt.

## Scope

Geeignet:

- Warum gewinnt eine bestimmte NixOS-Definition?
- Welcher Wert kommt bei `services.foo.enable` tatsächlich heraus?
- Welche Flake-Outputs existieren?
- Welche Derivation entsteht aus einem Installable?
- Was würde ein Nix-Build realisieren?
- Ist ein Problem tatsächlich Nix-semantisch?

Nicht geeignet:

- PR mergen;
- GitHub bearbeiten;
- Linux-Dienste reparieren;
- Bureau-Arbeit übernehmen;
- NixOS produktiv umschalten;
- Deployments durchführen.

Die vollständige Grenze steht in [`AUTHORITY.md`](AUTHORITY.md).

## Entwicklung

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Lokal per stdio:

```bash
.venv/bin/python server.py --transport stdio
```

HTTP bindet ausschließlich Loopback:

```bash
.venv/bin/python server.py --transport streamable-http --host 127.0.0.1 --port 18187
```

Der reale Fachpfad ist mit dem `heim-pc`-Flake belegt: `flake_show` und `flake_check --no-build` wurden über den Nixer-MCP erfolgreich gegen die gepinnte Backend-Image-ID ausgeführt.

## Lokaler Dienst und Tunnel

Nixer läuft als loopback-only User-Service auf `127.0.0.1:18187`. Die versionierte Unit liegt unter `deploy/nixer-mcp.service`.

Der dazugehörige OpenAI-Tunnel ist separat versioniert:

- `deploy/nixer.yaml` — Tunnelprofil mit MCP-Ziel `http://127.0.0.1:18187/mcp`;
- `deploy/tunnel-client-nixer.service` — User-Service für den Tunnel-Client.

Die ChatGPT-Plugin-/Connector-Erstellung bleibt bewusst eine manuelle Benutzeraktion, damit Name, Beschreibung und Bild im ChatGPT-UI gewählt werden können. Weder Nixer noch Grabowski erstellen den ChatGPT-Connector selbst.

Auf einem frischen Host müssen die versionierten Units zuerst in den systemd-User-Suchpfad verlinkt und das Tunnelprofil installiert werden. Der Tunnel-Service erwartet außerdem die lokal provisionierte Datei `~/.config/tunnel-client/grabowski-runtime.env` mit `CONTROL_PLANE_API_KEY`; Secret-Provisionierung ist nicht Teil dieses Repositories.

```bash
mkdir -p "$HOME/.config/tunnel-client"
install -m 0600 deploy/nixer.yaml "$HOME/.config/tunnel-client/nixer.yaml"
systemctl --user link "$PWD/deploy/nixer-mcp.service"
systemctl --user link "$PWD/deploy/tunnel-client-nixer.service"
systemctl --user daemon-reload
systemctl --user enable --now nixer-mcp.service tunnel-client-nixer.service
```
