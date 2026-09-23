{
  description = "Nixer — narrow Nix/Nixpkgs/NixOS specialist";

  # Nixer owns its runtime dependency set. Host flakes pin Nixer as a product;
  # they do not substitute their own Nixpkgs into Nixer's Python environment.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [ "x86_64-linux" ];
      forAllSystems = function:
        nixpkgs.lib.genAttrs supportedSystems (system:
          function (import nixpkgs { inherit system; }));

      packageFor = pkgs:
        let
          mcp130 = pkgs.python3Packages.mcp.overridePythonAttrs (old: {
            version = "1.30.0";
            src = pkgs.fetchPypi {
              pname = "mcp";
              version = "1.30.0";
              hash = "sha256-RFQUYl/OXClfqlBbsRus7OZhq29AKNV8k121eCC3o+Q=";
            };
            # The upstream SSE/WebSocket fixtures briefly release an ephemeral
            # 127.0.0.1 port before their child Uvicorn process binds it. In the
            # real system build, unrelated concurrent derivations share loopback
            # and can claim that port. Keep the transport tests, but isolate only
            # these listeners on a package-specific loopback alias and teach their
            # shared readiness helper to probe that explicit host.
            postPatch = (old.postPatch or "") + ''
              substituteInPlace tests/test_helpers.py \
                --replace-fail 'def wait_for_server(port: int, timeout: float = 20.0) -> None:' 'def wait_for_server(port: int, timeout: float = 20.0, host: str = "127.0.0.1") -> None:' \
                --replace-fail 's.connect(("127.0.0.1", port))' 's.connect((host, port))'
              substituteInPlace tests/shared/test_sse.py tests/shared/test_ws.py \
                --replace-fail '127.0.0.1' '127.0.0.2' \
                --replace-fail 'wait_for_server(server_port)' 'wait_for_server(server_port, host="127.0.0.2")'
              # Upstream sends ten in-memory requests, sleeps for a fixed 200 ms,
              # then drains only immediately available responses. Busy builders
              # can legitimately deliver responses after that deadline. Append
              # an in-band barrier request after the original ten. The session
              # processes this validation path serially and awaits each response
              # send, so seeing the barrier proves the preceding batch is drained.
              # This avoids both scheduler sleeps and output-stream EOS.
              substituteInPlace tests/issues/test_malformed_input.py \
                --replace-fail \
'            # Give time to process
            await anyio.sleep(0.2)

            # Verify we get error responses for all requests
            error_responses: list[Any] = []
            try:
                while True:
                    response_message = write_receive_stream.receive_nowait()
                    error_responses.append(response_message.message.root)
            except anyio.WouldBlock:
                pass  # No more messages' \
'            # Send an in-band barrier after the original batch. The validation
            # error path is processed serially, so its response is a completion
            # marker for every preceding malformed request.
            barrier_request = JSONRPCRequest(
                jsonrpc="2.0",
                id="malformed_barrier",
                method="initialize",
            )
            await read_send_stream.send(
                SessionMessage(message=JSONRPCMessage(barrier_request))
            )

            error_responses: list[Any] = []
            with anyio.fail_after(5):
                for _ in malformed_requests:
                    response_message = await write_receive_stream.receive()
                    error_responses.append(response_message.message.root)
                barrier_message = await write_receive_stream.receive()

            barrier_response = barrier_message.message.root
            assert isinstance(barrier_response, JSONRPCError)
            assert barrier_response.id == "malformed_barrier"
            assert barrier_response.error.code == INVALID_PARAMS

            # The barrier proves all preceding inputs were processed. Anything
            # still buffered now is an extra/duplicate response.
            with pytest.raises(anyio.WouldBlock):
                write_receive_stream.receive_nowait()'
            '';
            # pytest-xdist's Nixpkgs setup hook appends
            # --numprocesses=$NIX_BUILD_CORES after package pytestFlags. Disable
            # that hook for MCP so the explicit -n 0 below remains authoritative.
            # Keep the upstream transport coverage; only worker parallelism is
            # removed to prevent loopback port/session races.
            dontUsePytestXdist = true;
            pytestFlags = (old.pytestFlags or [ ]) ++ [ "-n" "0" ];
            # The real-build backend deliberately runs without SETUID/SETGID.
            # Consequently Nix builds as container-root; this upstream test
            # assumes chmod(000) is unreadable, which is false for root.
            disabledTests = (old.disabledTests or [ ]) ++ [ "test_permission_error" ];
          });
          python = pkgs.python3.withPackages (_ps: [ mcp130 ]);
        in
        pkgs.stdenvNoCC.mkDerivation {
          pname = "nixer";
          version = "0.2.0";
          dontUnpack = true;
          nativeBuildInputs = [ pkgs.makeWrapper ];
          dontBuild = true;

          installPhase = ''
            runHook preInstall
            mkdir -p "$out/bin" "$out/libexec/nixer"
            cp ${./server.py} "$out/libexec/nixer/server.py"
            cp ${./evidence.py} "$out/libexec/nixer/evidence.py"
            makeWrapper ${python}/bin/python "$out/bin/nixer" \
              --add-flags "$out/libexec/nixer/server.py"
            makeWrapper ${python}/bin/python "$out/bin/nixer-evidence" \
              --add-flags "$out/libexec/nixer/evidence.py"
            runHook postInstall
          '';

          meta = {
            description = "Nix-only specialist MCP and fixed evidence adapter for Nix/NixOS";
            mainProgram = "nixer";
            platforms = supportedSystems;
          };
        };
    in
    {
      packages = forAllSystems (pkgs: {
        default = packageFor pkgs;
        nixer = packageFor pkgs;
      });

      checks = forAllSystems (pkgs: {
        package = packageFor pkgs;
      });

      nixosModules.default = { config, lib, pkgs, ... }:
        let
          system = pkgs.stdenv.hostPlatform.system;
        in
        import ./nix/module.nix {
          inherit config lib pkgs;
          defaultPackage =
            if builtins.elem system supportedSystems then
              self.packages.${system}.default
            else
              throw "Nixer currently supports only: ${builtins.concatStringsSep ", " supportedSystems}";
        };
    };
}