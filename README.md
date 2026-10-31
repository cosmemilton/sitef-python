# sitef-python

SDK 0.2.0. Cliente Python 3.10+ assíncrono para CliSiTef, com ponte Rust em processo separado. Produção **Windows x64**. Biblioteca CliSiTef64I.dll e dependências oficiais fornecidas pelo integrador; DLL, INI, PDF, chaves e dados operacionais não acompanham o pacote. **Ainda não homologado financeiramente.**

O SDK oferece processamento semântico dos campos documentados, callback de interação e journal por pagamento. Ações que dependem de hardware ou regras do PDV exigem hooks explícitos. [Estado da implementação e limites](docs/implementation-status.md).

## Instalação

```powershell
python -m pip install .
cargo build --release --target x86_64-pc-windows-msvc --manifest-path native/Cargo.toml
```

Distribuição `cosmemilton-sitef`, import `sitef`, sem dependências de runtime além da biblioteca padrão. Ainda não publicado no PyPI. Instale o executável em pasta controlada pela aplicação e mantenha o runtime oficial separado.

## Sessão e interação

```python
from pathlib import Path
from sitef import Configuration, FieldHooks, TransactionInput, UiResponse, create_sitef

async def abrir(interagir, guardar, recuperar, imprimir):
    return await create_sitef(
        bridge_executable=Path(r"C:\Integracao\sitef-bridge.exe"),
        bridge_directory=Path(r"C:\Integracao"),
        library_path=Path(r"C:\Fornecedor\CliSiTef64I.dll"),
        state_directory=Path(r"C:\DadosPDV\sitef"),
        configuration=Configuration("127.0.0.1", "00000000", "AA123456"),
        interaction=interagir,
        hooks=FieldHooks(store_receipt=guardar, load_receipt=recuperar,
                         print_receipt=imprimir),
    )

async def vender(client, numero, data, hora, operador):
    return await client.transact(TransactionInput(0, "12,34", numero, data, hora, operador))
```

O callback é `async def handler(request, context) -> UiResponse`. Recebe comando, campo, limites, texto, bytes Base64 e contexto semântico em request.field_spec, request.typed_value e context.fields. Datas completas tornam-se tipos de data/hora; datas parciais mantêm componentes e decimais usam Decimal. Serialize esses tipos explicitamente se enviá-los a outro processo.

Retorne UiResponse(buffer="...") após coleta ou UiResponse(continue_flag=-1) para cancelar. Controle continue_flag=10000 exige confirmed=True para dado pré-coletado confirmado pelo operador, inclusive em comando desconhecido. Texto e Base64 da resposta devem corresponder byte a byte na codificação nativa. Menus anunciados como Mc exigem uma etapa separada de confirmação e UiResponse(buffer="1", confirmed=True). Comandos desconhecidos chegam integralmente ao callback. Comando zero preserva os bytes; on_exchange não substitui hooks obrigatórios. Comando23 continua sem aguardar o observador, preservando resposta do periférico.

Comando29 usa resolve_application_data quando disponível ou o callback da aplicação; esse pedido é dado do PDV, sem convertê-lo automaticamente em entrada humana. QR50/51/52 atualiza a tela sem aguardar confirmação. Para nosso modal, o renderer deve integrar **@cosmemilton/sitef-ui e cosmemilton-ui obrigatório** pelo canal privado da aplicação. O cliente Python não inclui navegador nem transporte de frontend.

Hooks aceitam funções síncronas ou assíncronas. Assinatura usual `(request, context)`, incluindo Receipt em persistência/impressão. Ações concluídas precisam retornar **True**; None não comprova sucesso. FieldHooks oferece:

- store_receipt, load_receipt, print_receipt;
- lock_resources, unlock_resources, capture_signature;
- resolve_field, resolve_application_data, on_diagnostic;
- accept_fallback, validate_transaction, resolve_confirmation_network;
- payment_data, store_finalization_payload, load_finalization_payload, attest_legacy_obligations.

## Comprovantes e ações obrigatórias

Padrão receipt_storage="persist": a aplicação guarda externamente cada recibo por ID estável antes de continuar. Administrativos/Corban imediatos exigem também print_receipt=True antes de continuar. Diferidos são impressos antes de finalize(True), seguidos de assinatura em papel5074. O journal guarda hash/reconhecimento, nunca corpo. load_receipt(metadata,payment) retorna Receipt ou {"body":texto,"bodyBase64":bytes_opcionais}; hash divergente impede confirmar, sem impedir desfazer. receipt_storage="explicit-return" entrega result.receipts e transfere a responsabilidade de armazenamento ao integrador, mas ainda exige impressão confirmada e recuperação externa após crash.

Restrições1042/1043/1044 são agrupadas por medicamento1011: violação numérica comprovada bloqueia confirmação, e unidade/escopo ausente exige validate_transaction com fields/productConstraints.1122/1123 validam quantidade e comprimento de código quando inequívocos; limites ambíguos exigem o mesmo hook. Carnê16/17 requer indicação2361/2363 ou accept_fallback=True. PBM4039 exige avaliação explícita quando sinaliza erro; diagnóstico4100 exige on_diagnostic. Bloqueio de recursos exige desbloqueio disponível e cleanup. Evento5010 é notificação; obrigação explícita de assinatura em papel é5074. Aprovação bancária já recebida com ação incompleta permanece pendente com confirmação bloqueada e possibilidade de desfazimento.

## Pagamentos e finalização

requires_finalization é inferido do catálogo. Contradizer função conhecida causa FINALIZATION_POLICY_MISMATCH; função ambígua/desconhecida exige booleano explícito antes de FFI. Valores monetários são strings exatas, como "12,34", nunca float.

```python
primeiro = await client.transact(dados_do_primeiro_pagamento)
segundo = await client.append_payment(dados_do_segundo_pagamento)
await client.finalize(True)  # decisão explícita do sistema fiscal
# Ou, se o índice161 foi recebido:
# await client.finalize_payment(True, primeiro.payment_id)
await client.close()
```

Append é explícito. Cupom diferente na mesma data é bloqueado enquanto há pendência; virada de data permite novo cupom. Finalização conjunta particiona por cupom/data, com hora original. finalize_payment exige índice161 único. finalize_network exige confirmation_network_field=131|158 ou resolver; um único campo131/158 não prova RedeConfirmacao. Seletores livres NumeroPagamentoCupom/RedeConfirmacao/NumeroPagamentoNFPAG/NFPAG são recusados em favor das APIs tipadas.

finalize_with_payments(confirm,payment_id,methods,total_amount) recebe PaymentMethod(tipo,valor,data={...}), valida730/731/732 e soma exata. methods=None usa payment_data(request,context). O índiceNFPAG escolhe quem recebe os dados; finaliza **todos os pagamentos do cupom/data**. Payload pode conter cheque: store_finalization_payload({id,parameters,hash},context) deve persistir externamente e retornar True. load_finalization_payload({id,hash},context) devolve a string exata; hash/escopo são verificados antes da recuperação. ID13 restringe redes e não é emitido; chequeNFPAG usa prefixos diferentes do comando31. Delimitadores sem escape documentado são recusados.

FinalizaFuncao é void: {"returned":True,"remoteConfirmation":"not-reported"} comprova somente retorno local, sem comprovar confirmação remota.

## Contexto, consultas e administração

FieldProcessor carrega os JSON comuns de536 campos e131 funções. Tipos compartilhando número são resolvidos pelo contexto. Campo2355 define formato da próxima coleta;2470 precisão da próxima30/34;1121/1126 precisão de preço/quantidade.109/137/138 preservam texto sem inferir unidade. Para aritmética use monetary_formats={137:"minor-units"} ou "decimal-comma", ou resolve_field retornando {"unscaled":"1234","scale":2}. Sem escala explícita, monetary_value bloqueia aritmética.

```python
resultado = await client.run_function(111)  # administrativa, dados fiscais vazios
pendencias = await client.query_pending_transactions()
especificas = await client.query_pending_transactions(
    fiscal_document="123", fiscal_date="20261030",
)
```

Consultas130/131 e funções administrativas não substituem o journal financeiro. Consulta agrupa160/161/163/164/211/1319 em ordem e verifica210. Valor de pendência permanece texto exato.

## Cancelamento e recuperação

Use client.cancel() ou cancel_event=asyncio.Event(). A sessão cancela a espera de interação, envia continueFlag=-1 e conduz o laço ao retorno terminal. Task.cancel(), timeout e fim do helper não comprovam cancelamento financeiro; podem deixar estado incerto. Não há replay/retry financeiro automático.

JournalV2 exclusivo por loja/terminal usa temporário, fsync e substituição atômica. Chave de arquivo SHA256(UTF8(storeId + NUL + terminalId)), compartilhável com Node/PHP no mesmo diretório. Todo o plano fica durável antes da primeira finalização; recuperação conserva decisão/escopo, sem repetir partições já finalizadas. V1 é migrado com legacyReceiptStateUnknown=True: confirmar exige attest_legacy_obligations(payment,context)=True certificando recibos, assinatura e obrigações comerciais; desfazer continua permitido. Corpo NFPAG legado só é removido após armazenamento externo confirmado.

Após reconectar, client.recovery e query_pending_transactions permitem conciliação. Depois da conferência fiscal/SiTef use finalize_recovery(decisao,registro["id"]). Plano seletivo preserva irmãos pendentes. Sem plano anterior, a decisão depende da aplicação. Locks antigos não são removidos automaticamente: confira processo/operação antes de liberar. BRIDGE_CLOSE_TIMEOUT preserva exclusividade se o helper não encerra normalmente.

## Auxiliares e validação

call_native(method,params) oferece somente métodos implementados na ponte, com nomes camelCase do protocolo, sem FFI arbitrária. Interativas usam NativeStart; identidade startBanking vem da sessão e continua obrigatória. Leitores nativos não financeiros aceitam identidade fiscal inteiramente vazia, sem herdar a modalidade nominal do input. capabilities() informa símbolos presentes e bloqueios. Contagens, teclas e presença não usam interpretação genérica de transação. Falhas de ciclo/consulta preservam retorno nativo em SitefError.native_status e campos/contexto em SitefError.result, sem incluí-los no repr da exceção. Strings precisam ser representáveis na codificação nativa e sem NUL; limites são em bytes. IPC UTF-8 tem limite2MiB, buffer padrão20000bytes.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
# Integração opcional com Rust e biblioteca C próprios:
cargo build --manifest-path native/Cargo.toml
PYTHONPATH=src SITEF_TEST_BRIDGE="$PWD/native/target/debug/sitef-bridge" \
  python3 -m unittest discover -s tests -v
```

O teste ABI opcional aceita SITEF_TEST_BRIDGE e, no Windows, SITEF_TEST_FIXTURE_LIBRARY com SITEF_TEST_OWNED_FIXTURE=1; use exclusivamente DLL compilada de native/tests/fixture.c, nunca a DLL do fornecedor. Testes exercitam subprocesso sintético, Unicode, correlação, frames inválidos, timeout/EOF, formatos, recibos, hooks, múltiplos pagamentos, escopos e recuperação. Linux/WSL test_mode=True é somente fixture de teste. scripts/smoke_windows.py executa apenas load/configuração/capacidades/versão com DLL externa. Isso não homologa pagamento, pinpad ou impressão física. Referências: PDFs VRS-258 e Carteiras Digitais1.43 do kitx86 auditado; nenhum PDFx64 usado.
