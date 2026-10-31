# sitef-python

Cliente Python 3.10+ assíncrono para integrar aplicações à CliSiTef por uma ponte Rust executada em processo separado. A biblioteca Python usa apenas a biblioteca padrão em runtime. Este projeto está em desenvolvimento e **não está homologado**.

O runtime de produção é **Windows x64**. A aplicação fornece o executável `sitef-bridge.exe`, a DLL de interface **CliSiTef64I.dll** e as dependências oficiais. DLLs, PDFs, INI, arquivos de chave, traces e dados de operação do fornecedor não acompanham o pacote. A ponte compartilhada é código próprio e usa stdin/stdout, sem shell nem porta de rede.

O catálogo comum contém os códigos disponíveis no projeto Node. Sua cobertura é declarada em `coverage`: há 444 entradas individuais de TipoCampo na tabela VRS-258, das quais somente 40 têm aliases nessa tabela; `fieldIds` possui 64 aliases ao incluir outros contextos. Os IDs de 124 funções dessa tabela estão catalogados, com sete funções adicionais de carteiras, mas seus fluxos não estão implementados/homologados individualmente. [Estado e limites](docs/implementation-status.md).

## Instalação e ponte

```powershell
python -m pip install .
```

O nome da distribuição é `cosmemilton-sitef`; o import é `sitef`. Ainda não há publicação em PyPI. A construção local usa setuptools e wheel, sem dependências adicionais de runtime.

A ponte é construída a partir do snapshot de código próprio em `native/`, com a toolchain Windows x64 apropriada:

```powershell
cargo build --release --target x86_64-pc-windows-msvc --manifest-path native/Cargo.toml
```

Instale o executável resultante em uma pasta controlada pela aplicação. O caminho absoluto da DLL é passado ao método `load`. O INI e os arquivos oficiais permanecem na instalação do integrador, conforme o fornecedor. `working_directory` afeta somente o processo auxiliar. A referência de protocolo continua sendo os PDFs x86 auditados, VRS-258 e Carteiras Digitais 1.43; os PDFs do kit x64 não foram usados.

## Operação com callback

Os nomes públicos Python usam `snake_case`; os argumentos enviados à ponte são mapeados para `camelCase`, preservando os valores originais. Valores monetários são **strings**, como `"12,34"`; a string vazia atende funções sem valor previamente definido. As strings de parâmetros adicionais são repassadas integralmente, sem whitelist de nomes.

```python
from pathlib import Path
from sitef import (
    Configuration, TransactionInput, UiResponse, create_sitef,
)

async def iniciar_sessao(interagir_com_usuario, imprimir_ou_observar):
    return await create_sitef(
        bridge_executable=Path(r"C:\Integracao\sitef-bridge.exe"),
        bridge_directory=Path(r"C:\Integracao"),
        library_path=Path(r"C:\Fornecedor\CliSiTef64I.dll"),
        state_directory=Path(r"C:\DadosPDV\sitef"),
        configuration=Configuration(
            server="127.0.0.1", store_id="00000000", terminal_id="AA123456",
            additional_parameters="[MultiplosCupons=1]",
        ),
        interaction=interagir_com_usuario,
        on_exchange=imprimir_ou_observar,
        native_encoding="windows-1252",
        timeout=120,
    )

async def conduzir_venda(client, dados_fiscais, operador):
    return await client.transact(
        TransactionInput(
            function_id=0, amount="12,34",
            fiscal_document=dados_fiscais.numero,
            fiscal_date=dados_fiscais.data_aaaammdd,
            fiscal_time=dados_fiscais.hora_hhmmss,
            operator=operador,
            additional_parameters="{ItemMenuIdentificado=1}",
        ),
        requires_finalization=True,
    )
```

`create_sitef` abre o helper, carrega a biblioteca externa, valida a versão do protocolo e a ABI Windows x64 e configura a sessão. Em caso de falha, encerra o helper. O nome do journal é `SHA256(UTF8(storeId + NUL + terminalId)) + ".journal.json"`, compartilhando a chave dos clientes Node/PHP quando usam o mesmo diretório de estado. Mantenha somente um dono do terminal por vez. `BridgeTransport`, `FileJournal` e `SitefClient.connect` permanecem disponíveis para integrações avançadas e transportes próprios.

Depois do retorno, a aplicação verifica `result.status` e o estado fiscal. Quando houver `awaiting-finalization`, ela toma a decisão explícita de confirmar ou desfazer:

```python
await client.finalize(confirm=decisao_do_sistema_fiscal)
await client.close()
```

`finalize` invoca uma função nativa `void`. Seu retorno `{"returned": True, "remoteConfirmation": "not-reported"}` indica somente que a chamada local retornou. Sem escopo adicional, a finalização alcança todos os pagamentos do mesmo cupom/data. É possível fornecer `additional_parameters="{NumeroPagamentoCupom=2}"` ou outros parâmetros documentados, preservados na recuperação.

**`requires_finalization` é obrigatório em cada operação.** Use a política da modalidade: pagamentos podem exigir `True`, enquanto consultas e testes geralmente usam `False`. O pacote não deduz essa decisão a partir do nome ou do ID da função. Nenhuma modalidade é repetida automaticamente.

O callback é `async def handler(request, context) -> UiResponse`. Ele recebe comando, tipo de campo, mínimo/máximo, buffer, bytes em Base64, indicador de erro de decodificação e a resposta bruta da ponte. `context.cancelled` e `context.cancel_event` permitem cancelar uma tela pendente. Retorne `UiResponse(buffer="...")` após coleta ou `UiResponse(continue_flag=-1)` para solicitar cancelamento nativo. Todos os códigos numéricos desconhecidos continuam disponíveis ao integrador.

O comando `0` passa pelo observador assíncrono `on_exchange`; a sessão preserva sua sequência e ecoa os mesmos bytes. Use esse observador para processar/imprimir comprovantes quando a modalidade exigir impressão imediata. Uma falha de impressão deve interromper o fluxo pelo cancelamento; somente guardar os campos 121/122 não cumpre essa obrigação. O comando `23` continua imediatamente e seu observador é disparado sem bloquear o periférico. Os callbacks precisam cooperar com o cancelamento e não bloquear o event loop.

## Interface própria ou modal compartilhado

Uma aplicação Python pode implementar suas telas no callback. Para usar o nosso modal, o renderer da aplicação integra **`@cosmemilton/sitef-ui` e sua dependência obrigatória `cosmemilton-ui`**, por um canal privado da aplicação. A biblioteca Python não importa componentes JavaScript e não inclui um navegador ou servidor HTTP.

O backend envia o pedido de interação ao renderer e aguarda a resposta correlacionada. O renderer encaminha `buffer` e `continueFlag`, além de cancelamentos, para o backend. Valide origem, identidade da operação e formato das mensagens no canal da aplicação. Envie ao renderer somente os dados necessários à tela; buffers de comprovante, cartão e dados de recuperação permanecem sob controle do backend. O pacote não fornece esse transporte de frontend nem declara um modal Python pronto. O projeto Node fornece o controller e a referência de renderer para esse fluxo.

O comando `29` exige dados da aplicação, sem converter automaticamente a solicitação em digitação humana. Os comandos QR `50`, `51` e `52` atualizam a tela enquanto o laço continua; não aguardam confirmação de uma pessoa. Comando desconhecido exige tratamento explícito do callback. A documentação de produtos continua necessária para interpretar o tipo de campo.

## Cancelamento, falhas e recuperação

Use `client.cancel()` ou forneça `cancel_event=asyncio.Event()` a `transact`. A sessão interrompe a espera de callback e envia `continueFlag=-1`, conduzindo o laço até retorno terminal. Abortar uma Task com `Task.cancel()`, ocorrer timeout, encerrar o processo ou fechar uma tela não comprova cancelamento financeiro; nesses casos o journal pode ficar em `uncertain`.

O journal é gravado antes da primeira chamada financeira, com exclusividade por arquivo, escrita temporária, `fsync` e substituição atômica. Reutilize o mesmo caminho local persistente por loja/terminal. Ele armazena identidade, estado, decisão e escopo de finalização, sem registrar buffers de coleta ou comprovante. Não use a string de finalização para gravar dados de cartão ou senhas. Um lock deixado após crash permanece bloqueando o terminal até a aplicação/operador conferir o processo e reconciliar a operação; o SDK nunca remove locks antigos automaticamente.

Ao reconectar, uma operação sem estado terminal bloqueia novas transações. Consulte `client.recovery`, `pendingCount` e, quando necessário, as funções 130/131 no fluxo de recuperação definido pela aplicação. O cliente permite consultas diretas enquanto está incerto; iniciar uma nova função iterativa nesse estado exige uma estratégia de reconciliação externa. Após a conferência:

```python
pending = await client.call_native("pendingCount", {
    "fiscalDate": registro["fiscalDate"],
    "fiscalDocument": registro["fiscalDocument"],
})
# A decisão pertence à aplicação, após conciliação fiscal/SiTef.
await client.finalize_recovery(decisao_confirmada, registro["id"])
```

A retomada preserva a decisão e o escopo já persistidos e recusa sua troca. Uma falha de transporte não reinicia o helper nem reenfileira a operação financeira. Se o helper precisar ser encerrado à força em `close`, o erro `BRIDGE_CLOSE_TIMEOUT` mantém a exclusividade do journal; confira o processo antes de tentar liberar o terminal.

## API auxiliar e limites

`client.call_native(method, params)` chama somente o contrato tipado implementado na ponte; ela não oferece FFI arbitrária. Os parâmetros desse escape hatch usam os nomes **camelCase do protocolo**, por exemplo `pendingCount`, `pinpadMessage`, `readPasswordDirect`. Rotinas que iniciam um laço usam `NativeStart` em `transact`; a identidade de `startBanking` é sempre a identidade fiscal registrada pela sessão. `validate_barcode` também pode ser usado durante coletas 31/35.

`catalog()` retorna uma cópia do JSON comum; `command_info()` oferece metadados de comando. `describe_return()` cobre os mapas genéricos de configuração, transação e leitura segura, conservando o código original. Nos demais contextos, devolve `kind="raw"`: contagens, presença, teclas e confirmações do pinpad exigem a interpretação específica da rotina. Não trate qualquer retorno positivo como recusa de pagamento.

O buffer nativo aceita 20.000 a 1.048.576 bytes, e o frame IPC aceita até 2 MiB. Texto e Base64 da mesma resposta ocupam o frame juntos; por isso um buffer de 1 MiB não garante que sua resposta completa caiba no frame. O padrão de 20.000 bytes evita esse extremo. Resposta excessiva ou malformada encerra o canal e exige reconciliação. Comprimentos de campo usam bytes da codificação nativa; NUL interno e perda na codificação de entrada são rejeitados. O encoding do cliente deve coincidir com `transport.load`.

## Testes

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Há testes de subprocesso real com host sintético próprio, sem DLL de fornecedor, cobrindo Unicode, frames excessivos/truncados, ID errado, timeout, EOF, cancelamento, crash, journal e recuperação. No WSL/Linux, o modo `test_mode=True` permite apenas a finalidade de testes sintéticos. Isso não constitui suporte a runtime Linux.

O script `scripts/smoke_windows.py` aceita `--bridge`, `--library` e `--state-directory` absolutos e executa somente criação/configuração da sessão e consulta de versão. Ele não solicita transação nem operação de pinpad; a biblioteca continua externa ao projeto.

Teste opcional com a ponte Rust real e a fixture C própria do snapshot:

```bash
cargo build --manifest-path native/Cargo.toml
PYTHONPATH=src SITEF_TEST_BRIDGE="$PWD/native/target/debug/sitef-bridge" \
  python3 -m unittest discover -s tests -v
```

Esses testes comprovam o contrato do componente e do processo auxiliar. Transações reais, pinpad e homologação de cada produto exigem validação adicional.
