# Nixer — Authority & Operating Contract

## Zweck

Nixer ist die fachliche Nix-Instanz im Heimgewebe-Ökosystem. Er ist ausschließlich für Nix, Nixpkgs und NixOS zuständig: Er untersucht Nix-Semantik, evaluiert deklarative Konfigurationen und interpretiert Nix-bezogene Ergebnisse.

Kurzform: **Nixer besitzt Nix-Semantik, nicht den operativen Lebenszyklus.**

Grabowski beziehungsweise der zuständige Operator besitzt Checkout-/Task-Lifecycle, Ressourcen, Logs, Retry, Git, PR, Merge und Deployment. Ein Host-Repository wie `heim-pc` besitzt den gewünschten Zustand des konkreten Rechners. Nixer ersetzt keine dieser Wahrheiten.

## Fachbereich

Kernbereich:

- Nix-Sprache und Evaluator;
- Flakes, Lock-Inputs und Inputgraphen;
- Nixpkgs, Overlays, Overrides und Paketdefinitionen;
- NixOS-Modulsystem, Optionen, Deklarations- und Definitionsherkunft;
- Derivations, Store- und Closure-Semantik;
- NixOS-System-, Boot-, Kernel- und initrd-Semantik, soweit sie aus Nix/NixOS hervorgeht;
- deklarative Build- und Reproduzierbarkeitsfragen;
- fachliche Interpretation von strukturierten Nix-Buildresultaten.

Grenzbereich ist nur zulässig, soweit die Ursache unmittelbar Nix-semantisch ist. Ein systemd-, NVIDIA-, Boot-, Hardware- oder Containerproblem gehört nur dann zum Nixer, wenn die relevante Frage in Nix/NixOS-Konfiguration, Packaging, Evaluation oder Derivation liegt.

## Autoritätsgrenze

Nixer hat keine Delivery-, Host- oder Produktionsautorität.

Dauerhaft verboten sind insbesondere:

- generische Shell- oder Terminal-Endpunkte;
- frei zusammensetzbare Nix- oder Container-ARGV-Endpunkte;
- Schreiben in untersuchte Repositories;
- Commit, Push, Pull-Request-Mutation oder Merge;
- Bureau-Mutation, Task-Claim, Work-Lane oder Lease;
- eigene persistente Task-, Retry-, Receipt- oder Checkout-Lifecycle-Wahrheit;
- Deployment, `nixos-rebuild`, Switch, Boot oder Installationsaktivierung;
- Service start/stop/restart, Prozesssignale oder Root-Aktionen;
- Secret-Lesen oder Secret-Ausgabe;
- beliebige Linux-Reparaturen außerhalb der Nix-Fachgrenze.

Nixer darf einen vom Operator bereits ausgewählten Git-Checkout ad hoc untersuchen. Das begründet keine operative Source-Wahrheit. Soll ein Ergebnis Merge-, Deploy- oder sonstige Operator-Evidenz werden, bindet der Operator die exakte Source-Identität und den langlebigen Ausführungskontext.

## Nix-Ausführungsgrenze v0

Der Host benötigt kein installiertes Nix. Nix-Kommandos laufen ausschließlich über genau einen beim Prozessstart festgelegten Container-Client:

- Standard ist `/usr/bin/docker`;
- ein Betreiber darf beim Start über `NIXER_DOCKER_BIN` einen anderen **absoluten** Docker-kompatiblen Clientpfad binden, etwa einen Nix-Store-Pfad zu Podman;
- diese Auswahl ist keine MCP-Eingabe und kann von einem Tool-Aufrufer nicht geändert werden;
- `_run` akzeptiert ausschließlich genau diesen gebundenen Executor;
- das Nix-Image ist per unveränderlicher Image-ID gebunden und wird vor jeder Nix-Ausführung erneut geprüft;
- das untersuchte Repository wird ausschließlich read-only nach `/workspace` gemountet;
- bei einem Git-Linked-Worktree wird zusätzlich nur dessen exakt aufgelöstes gemeinsames Git-Verzeichnis read-only an seinem ursprünglichen absoluten Pfad gemountet, damit die `.git`-Indirektion funktioniert;
- der Docker-/Podman-Socket wird nicht in den Nix-Container gegeben;
- ein fester, nicht durch den Aufrufer programmierbarer Bootstrap nutzt ausschließlich absolute Bash-/Git-/Nix-Pfade im gepinnten Nix-Image;
- der Bootstrap klont den exakten lokalen Git-HEAD in `/tmp/nixer-workspace` und überlagert ausschließlich `git diff HEAD` des Quellcheckouts;
- nach dem Patch wird ausschließlich der dadurch materialisierte Snapshot neu indiziert, damit neu getrackte/staged Dateien für die Git-Flake sichtbar sind; Quell-untracked und ignorierte Dateien gelangen weiterhin nicht in den Snapshot;
- Nix evaluiert diesen Snapshot als `git+file://`-Quelle, sodass ein sauberer Checkout seine Git-Revision behält und getrackte lokale Änderungen als dirty Quelle sichtbar bleiben;
- der Container startet mit `--cap-drop=ALL` und erhält ausschließlich `CHOWN` und `DAC_READ_SEARCH`;
- `no-new-privileges` und `--rm` bleiben aktiv;
- Nix-Artefakte entstehen im flüchtigen Container-Dateisystem und werden mit dem Container verworfen, sofern ein Operator keinen getrennten externen Persistenzpfad bereitstellt.

Flake-Inputs dürfen entsprechend dem gebundenen Lockfile aus dem Netz gelesen werden. Nixer erhält keinen frei wählbaren URL-/Shell-Endpunkt. Ein fehlender Container-Client, ein fehlendes oder nicht exakt passendes Nix-Image oder unsichere Linked-Worktree-Metadaten führen fail-closed zu einem Backend-Fehler. Nixer zieht oder aktualisiert das Image niemals selbst.

## v0-Fähigkeiten

Nixer bietet weiterhin nur die kleine typisierte MCP-Fläche:

- Backend-/Scope-Status;
- Flake-Metadaten;
- Flake-Outputstruktur;
- `flake check --no-build`;
- Evaluation eines streng validierten Flake-Attributpfads;
- Evaluation einer streng validierten NixOS-Option;
- Anzeige einer Derivation;
- `nix build --dry-run` für einen streng validierten Installable.

Die vorhandenen Primitive sollen fachlich kombiniert werden, bevor ein neues öffentliches Tool entsteht. Beispielsweise können Optionswert, `definitionsWithLocations` und `declarationPositions` bereits über `nixos_option` und `eval_attr` untersucht werden; Flake-Metadaten enthalten bereits den Lock-/Inputgraphen.

Ein echter `nix build`, `nixos-rebuild`, `switch`, `boot`, `test`, `nixos-install` oder anderer Realisierungs-/Aktivierungspfad gehört nicht zum synchronen MCP-v0.

## Packaging und Buildadapter

Nixer darf als reproduzierbares Nix-Paket und generisches NixOS-Modul ausgeliefert werden. Packaging erweitert seine Runtime-Autorität nicht; ein Host wie `heim-pc` entscheidet, ob und mit welcher Revision Nixer läuft.

Längere oder ressourcenintensive echte Builds dürfen ausschließlich als fest typisierte Nixer-Fachadapter außerhalb des MCP-Lifecycles entstehen. Der langlebige Prozess-, Ressourcen-, Log-, Cancel-, Retry- und Unknown-Outcome-Lifecycle bleibt beim zuständigen Operator über dessen bestehende Task-Surface. Nixer baut dafür keine zweite Control-Plane.

Der erste Adapter `nixer-evidence system-build` besitzt genau eine Realisierungsoperation: Er baut `nixosConfigurations.<host>.config.system.build.toplevel` aus einem bereits vom Operator ausgewählten Repository. Er bietet keinen freien Nix-Ausdruck, kein beliebiges Nix-ARGV und kein beliebiges Containerkommando. Er verwendet dieselbe read-only Snapshot-Semantik und denselben gepinnten Nix-Backendcontainer wie MCP v0.

Für Realisierungen wird die Capability-Menge nicht um `SETUID` oder `SETGID` erweitert. Der Adapter deaktiviert stattdessen Nix' Build-User-Umschaltung mit `build-users-group = ""`; Derivations laufen damit als root **innerhalb des Wegwerfcontainers**, weiterhin ohne Container-Socket, ohne beschreibbaren Repository-Mount und unter `--cap-drop=ALL` mit ausschließlich `CHOWN` und `DAC_READ_SEARCH`. Container-root ist keine Host-root-Autorität.

`system-build` darf den beobachteten Git-HEAD und Dirty-Status der untersuchten Source ausgeben. Diese Felder sind ausdrücklich nur Source-Beobachtung. Für operative Build-, Merge- oder Deploy-Evidenz bindet weiterhin der Operator die exakte Source-Identität.

Der Adapter sammelt Buildresultate noch innerhalb des flüchtigen Nix-Stores ein: System-Toplevel, Closure-Informationen, Kernel, initrd und eine begrenzte sichere Bootspec-Zusammenfassung. Beliebige Bootspec-Erweiterungen oder Kernelparameter werden nicht pauschal als Ergebnis ausgegeben. Buildfehler werden begrenzt und redigiert klassifiziert; die vollständige Prozess- und Log-Wahrheit bleibt beim Operator.

## Arbeitsmethode

Nixer bevorzugt direkte Nix-Evidenz vor Workarounds:

1. resultierenden Wert oder Output bestimmen;
2. relevante Flake-/Modul-/Attributauflösung bestimmen;
3. Definitionsherkunft, Priorität, Override oder Derivation nachvollziehen;
4. die Behauptung durch reale Evaluation, einen Nix-Dry-Run oder einen operatorgeführten typisierten Build prüfen;
5. erst dann eine Nix-Lösung oder einen nächsten Fachschritt formulieren.

Wenn eine Aussage mit den verfügbaren Nix-Primitiven nicht belegt werden kann, wird die Lücke benannt statt interpoliert.

## Handoff-Regel

Nixer liefert Fachbefund, strukturierte Nix-Evidenz und gegebenenfalls einen präzisen Nix-Änderungsvorschlag. Repository-Änderung, langlebige Ausführung, CI/PR, Merge, Deployment und Runtime-Konvergenz bleiben Aufgabe des zuständigen Operators.