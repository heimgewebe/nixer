# Nixer

Nixer ist der eng begrenzte Nix-Spezialist im Heimgewebe-Operator-Ökosystem.

> Er macht nichts außer Nix. Nix soll er dafür außergewöhnlich gut können.

Nixer besitzt fachliche Nix-/Nixpkgs-/NixOS-Semantik, aber keinen operativen Lebenszyklus: keine Git-/PR-/Bureau-/Deploy-Autorität, keine eigenen langlebigen Tasks und keine Host-Aktivierung. Diese Grenze ist in [`AUTHORITY.md`](AUTHORITY.md) festgeschrieben.

## MCP v0

Die öffentliche Fläche bleibt bewusst klein:

- `nixer_status` — Scope, Verbote und Backend-Bereitschaft;
- `flake_metadata` — reale Flake-Metadaten einschließlich Lock-/Inputgraph;
- `flake_show` — reale Outputstruktur eines Flakes;
- `flake_check` — Evaluation per `nix flake check --no-build`;
- `eval_attr` — einen streng validierten Flake-Attributpfad evaluieren;
- `nixos_option` — einen resultierenden NixOS-Optionswert zusammen mit begrenzten Typ-, Deklarations- und Definitionsort-Metadaten atomar evaluieren;
- `derivation_show` — resultierende Derivation anzeigen;
- `build_dry_run` — Buildplan prüfen, ohne den Output zu realisieren.

Neue Diagnosefragen werden zunächst durch Kombination dieser Primitive gelöst, nicht durch ein neues Tool pro Frage. `nixos_option(repo, host, option)` behält seine Eingabesignatur unverändert und liefert Wert, Typ sowie Deklarations- und Definitionsorte aus einer einzigen serverdefinierten Nix-Evaluation desselben Snapshots. Die Werte aus `definitionsWithLocations` werden dabei nicht ausgegeben. Herkunftslisten sind serverseitig klein begrenzt; `declaration_positions_truncated` beziehungsweise `definition_locations_truncated` kennzeichnen, wenn die jeweilige Liste wegen dieser Grenze gekappt wurde.

## Ausführungsmodell

Nix läuft in einem flüchtigen Container mit gepinntem Nix-Image. Der Container-Client wird genau einmal beim Nixer-Prozessstart gebunden:

- Standard: `/usr/bin/docker`;
- optional: absoluter, betreiberseitig gesetzter Pfad über `NIXER_DOCKER_BIN`, etwa ein Nix-Store-Pfad zu Podman;
- der MCP-Aufrufer kann den Executor nicht wählen oder verändern;
- `_run` akzeptiert ausschließlich genau diesen einen gebundenen Client;
- das Nix-Image bleibt an seine unveränderliche Image-ID gebunden;
- das untersuchte Repository wird read-only unter `/workspace` eingehängt;
- bei Git-Linked-Worktrees wird nur das exakt aufgelöste gemeinsame Git-Verzeichnis zusätzlich read-only gemountet;
- der Bootstrap klont HEAD, überlagert ausschließlich `git diff HEAD` und indiziert nur den dadurch entstandenen Wegwerf-Snapshot; Quell-untracked und ignorierte Dateien bleiben ausgeschlossen;
- keine Container-Socket-Mounts in den Nix-Container;
- `--cap-drop=ALL`, nur `CHOWN` und `DAC_READ_SEARCH`, `no-new-privileges`, `--rm`;
- keine echten Builds oder Aktivierungen im synchronen MCP-v0.

Gebundenes Nix-Image:

```text
sha256:98edc6813218e179ce84587373e0b52d4aa58babae2d26b51fb01e7fdacf815f
```

Das entspricht `nixos/nix:2.35.2`. Nixer zieht oder aktualisiert das Image nie selbst und scheitert bei fehlender oder abweichender Identität fail-closed.

## Nix-Paket und NixOS-Modul

Nixer ist zusätzlich selbst als Flake paketiert:

```text
packages.x86_64-linux.default
packages.x86_64-linux.nixer
nixosModules.default
checks.x86_64-linux.package
```

Das Paket installiert `nixer` für den MCP-Dienst und `nixer-evidence` für fest typisierte, operatorgeführte Realisierungsabläufe. Das NixOS-Modul stellt `services.nixer` bereit und erzeugt einen loopback-only systemd-User-Service. Host-spezifische Policy — konkrete Aktivierung, Port, Container-Client, Tunnel und Ressourcen — gehört in das jeweilige Host-Repository, nicht in die Nixer-Fachlogik.

Beispiel:

```nix
{
  imports = [ inputs.nixer.nixosModules.default ];
  services.nixer = {
    enable = true;
    port = 18187;
  };
}
```

Das Modul verwendet standardmäßig `${pkgs.podman}/bin/podman` als absoluten Container-Client; ein Host kann `services.nixer.containerCli` explizit überschreiben. Das gepinnte Nix-Image muss im gewählten Runtime-Store bereits vorhanden sein.

## Evidence-Adapter v1

Der erste Realisierungsadapter bleibt bewusst außerhalb des MCP-Lifecycles:

```text
nixer-evidence system-build --repo /path/to/repo --host heim-pc
```

`system-build` ist fest auf `nixosConfigurations.<host>.config.system.build.toplevel` gebunden. Es gibt keinen freien Nix-Ausdruck, kein beliebiges Nix-ARGV und kein beliebiges Containerkommando. Der Adapter verwendet dieselbe read-only Snapshot-Semantik und dasselbe gepinnte Nix-Image wie MCP v0, realisiert den System-Toplevel im flüchtigen Container-Store und gibt ein strukturiertes JSON-Ergebnis zurück.

Für Realisierungen bleibt die Container-Capability-Menge eng: `SETUID` und `SETGID` werden nicht ergänzt. Der Evidence-Container erhält zusätzlich zu `CHOWN` und `DAC_READ_SEARCH` genau `FOWNER` und `DAC_OVERRIDE`: `FOWNER` ist für Nix' Modusänderungen an numerischen Build-UIDs nötig; `DAC_OVERRIDE` ist für das anschließende Aufräumen fremdbesessener Buildbäume nötig. Beide Lücken wurden gegen die konkreten `chmod`-/`unlink`-Fehler im Wegwerfcontainer einzeln reproduziert. Repository-Mount und Git-Metadaten bleiben read-only, der Docker-Socket bleibt abwesend, `no-new-privileges` und `--cap-drop=ALL` bleiben aktiv. Nix läuft zugleich mit deaktivierter Build-User-Umschaltung (`build-users-group = ""`). MCP v0 behält seine kleinere Capability-Menge ohne `FOWNER` und `DAC_OVERRIDE`.

Die Ausgabe enthält insbesondere:

- beobachteten Git-HEAD und Dirty-Status der untersuchten Source — ausdrücklich nur Beobachtung, keine Source-Authority;
- realisierten System-Toplevel;
- Closure-Größe und `path-info`;
- Kernel- und initrd-Storepfad;
- eine begrenzte sichere Zusammenfassung von `boot.json`, falls vorhanden;
- bei Fehlern eine begrenzte Nix-spezifische Fehlerklassifikation.

Der langlebige Prozess gehört weiterhin dem Operator. In Heimgewebe startet daher Grabowski `nixer-evidence` als Task und besitzt Laufzeit, CPU/RAM/IO, Logs, Cancel, Retry und Unknown-Outcome-Behandlung. Nixer führt dafür keine eigene Task-, Lease-, Receipt- oder Retry-Wahrheit ein.

## Geeignet

- resultierende NixOS-Optionen und ihre Definitionsherkunft untersuchen;
- Flake-Inputs, Lockgraphen und Outputs auswerten;
- Overlays, Overrides und Derivationen nachvollziehen;
- Buildpläne per Dry-Run prüfen;
- einen operatorgebundenen NixOS-Systembuild fachlich realisieren und auswerten;
- entscheiden, ob ein Fehler tatsächlich Nix-semantisch ist.

## Nicht geeignet

- Repositories verändern oder PRs mergen;
- Bureau-Arbeit oder Work-Lanes übernehmen;
- langlebige Jobs selbst verwalten;
- beliebige Nix-/Shell-/Container-Kommandos ausführen;
- NixOS produktiv umschalten oder installieren;
- Dienste reparieren oder Deployments durchführen.

## Entwicklung

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check server.py evidence.py tests
```

Lokal per stdio:

```bash
.venv/bin/python server.py --transport stdio
```

HTTP bindet ausschließlich Loopback:

```bash
.venv/bin/python server.py --transport streamable-http --host 127.0.0.1 --port 18187
```

Die Flake-Evaluation ist auch aus Grabowski-Linked-Worktrees unterstützt; deren Git-Common-Dir wird nur read-only für die Snapshot-Erzeugung sichtbar gemacht.

## Übergangs-Deployment

Die bisherigen Dateien unter `deploy/` bleiben vorerst erhalten, weil der derzeit laufende Dienst noch daraus installiert ist. Sie sind eine Übergangsoberfläche, bis der konkrete Host Nixer über `nixosModules.default` und eine gepinnte Flake-Revision betreibt.

Der OpenAI-Tunnel bleibt eine separate Host-/Betriebsintegration. Secret-Provisionierung und ChatGPT-Connector-Erstellung gehören ausdrücklich nicht zum Nixer-Repo.