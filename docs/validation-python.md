# Validação do cliente Python 0.2.0

Em 30/10/2026, a suíte completa passou com 58 testes no Python3.12.3/WSL e 58 no Python3.13/Windowsx64, sem skips nas execuções com fixture nativa configurada.

Os testes verificam subprocessos próprios, correlação e limites IPC, codificação CP1252, cancelamento cooperativo, falha de transporte, formatos2355/precisão2470, contexto seguro por entrypoint, ações obrigatórias, recibos, restrições de produto, múltiplos pagamentos, partições por cupom/data, filtros161/rede, consulta130/131, integridade e escopo de recuperação, NFPAG externo e migração/atestação de V1.

A integração ABI executou Python → ponte Rust própria → biblioteca compilada de native/tests/fixture.c, tanto .so Linux quanto DLL Windowsx64. Essa biblioteca é uma fixture original de contrato, não contém implementação do fornecedor e nunca foi colocada no pacote distribuído. Seu ciclo simulado testa CP1252 e finalização local; os callbacks de armazenamento/impressão retornam sucesso artificialmente para testar o contrato.

A quantidade de testes não demonstra execução de todos os536 campos,131 funções ou autorizadores. A checagem de cobertura semântica confere o catálogo; casos de comportamento representam classes de formato, contexto, ação e recuperação. Não houve pagamento real, pinpad físico, impressora física ou homologação financeira. O smoke já realizado com o cliente simulado do fornecedor limitou-se a carregar/configurar e consultar capacidades/versão.

Reprodução Linux/WSL, somente fixture própria:

```bash
cargo build --manifest-path native/Cargo.toml
PYTHONPATH=src SITEF_TEST_BRIDGE="$PWD/native/target/debug/sitef-bridge" \
 python3 -m unittest discover -s tests -v
```

Reprodução Windows, apontando exclusivamente para artefatos próprios compilados separadamente:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
$env:SITEF_TEST_BRIDGE = 'C:\ArtefatosProprios\sitef-bridge.exe'
$env:SITEF_TEST_FIXTURE_LIBRARY = 'C:\ArtefatosProprios\fixture.dll'
$env:SITEF_TEST_OWNED_FIXTURE = '1'
python -m unittest discover -s tests -v
```

Não aponte SITEF_TEST_FIXTURE_LIBRARY para CliSiTef64I.dll. A suíte opcional exercita valores sentinela definidos exclusivamente na fixture C própria.
