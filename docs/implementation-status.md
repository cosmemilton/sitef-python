# Estado da implementação

SDK 0.2.0. Cliente asyncio da ponte Rust compartilhada em processo separado. Produção: Windows x64. Biblioteca CliSiTef64I.dll fornecida pelo integrador. O projeto ainda não foi homologado financeiramente.

## Catálogo e semântica

`protocol/fields.json` contém 536 entradas: os 444 IDs individuais da seção 5.3.2 da VRS-258, 64 eventos e 28 campos suplementares, além de sete intervalos contextuais. `protocol/functions.json` descreve 131 funções: os 124 IDs da seção 5.2.2 e sete adicionais de carteiras. Os arquivos são empacotados em `sitef/data/`, sem tabela Python manual concorrente.

`FieldProcessor` aplica tipos, formatos, precisão, contexto e obrigações desse metadado ao ciclo. Datas parciais mantêm componentes conhecidos sem inventar século. Decimais usam `Decimal` ou strings exatas, nunca `float`. Os campos 109/137/138 permanecem texto até `monetary_formats` ou `resolve_field` informar unidade/escala. Precisão 2470 é consumida pela próxima coleta 30/34; 1121/1126 ficam nos contextos de preço unitário/quantidade. Dicas 2355 sobrevivem a mensagens intermediárias. `Mc` exige confirmação separada e `UiResponse.confirmed=True`.

Restrições1042/1043/1044 são avaliadas por medicamento1011, sem misturar produtos. Comparação comprovadamente inválida não pode ser liberada pelo hook; escala/índice/comparando ausentes exigem validate_transaction.1122/1123 validam contagem/comprimento de código quando inequívocos; zero ou semântica exata/máxima ambígua exige avaliação explícita. Limites de parcelas declarados no catálogo também exigem validação comercial. O contexto de avaliação contém fields ordenados e productConstraints.

Hardware, dados do PDV, supervisor e regras comerciais exigem os hooks declarados no catálogo. Ausência/falha provoca cancelamento cooperativo. Se o banco já retornou zero, a operação permanece pendente, com confirmação bloqueada e possibilidade de desfazimento. Eventos5010 são notificações de assinatura; apenas5074 define a obrigação universal de assinatura em papel. Campos desconhecidos e repetições continuam disponíveis integralmente.

## Journal V2 e recuperação

O journal guarda pagamentos separados, decisão e escopo por pagamento. `append_payment` é explícito; cupom diferente na mesma data é bloqueado enquanto há pendência. A virada de data admite novo cupom. Finalização conjunta particiona cupom/data e usa a hora original. Todo o plano é persistido antes da primeira FFI; partições já finalizadas não são reenviadas após falha.

`finalize_payment` exige índice161 único. `finalize_network` exige política explícita: a correspondência de RedeConfirmacao com131/158 não é inferida quando ambígua. Seletores livres são recusados em favor dessas APIs. `finalize_with_payments` constrói NFPAG com soma exata, limites730, tipos731, requisitos732, restriçãoID13, origem correta de cheque e terminador obrigatório. Mesmo com NumeroPagamentoNFPAG, finaliza toda a partição do cupom/data.

Corpos de comprovantes ficam no armazenamento da aplicação. O journal contém somente ID, tipo, hash e confirmação de impressão. Antes de confirmar: imprimir comprovantes diferidos, concluir assinatura5074 e persistir reconhecimentos. Após reinício, `load_receipt` deve fornecer corpo com hash correto. Ausência/divergência impede confirmar; desfazer continua permitido. Impressão já reconhecida não se repete. O integrador deve usar o ID estável para idempotência na janela entre impressão física e registro durável.

NFPAG pode conter dados de cheque. `store_finalization_payload` guarda o conteúdo externamente e retorna True; o journal persiste apenas `{id,hash}` e seletor seguro. `load_finalization_payload` restaura a string, cuja integridade e escopo são verificados antes de qualquer FFI de recuperação. Journal V1 é lido como V2 com legacyReceiptStateUnknown=True: confirmar requer attest_legacy_obligations=True certificando recibos, assinatura e obrigações comerciais desconhecidas; desfazer continua permitido. Corpos NFPAG legados só são removidos após confirmação do armazenamento externo; sem hook/falha, o arquivo original permanece intacto.

Consultas iterativas130/131 são permitidas em estado incerto ou com pagamentos pendentes. A leitura ordenada210/160/161/163/164/211/1319 não substitui o journal financeiro. `run_function` permite funções administrativas com dados fiscais vazios quando aplicável. A política de finalização é inferida do catálogo: contradição explícita é recusada; função desconhecida/ambígua exige decisão explícita antes de FFI.

## Dependências concretas e validação pendente

- Impressora, armazenamento externo, assinatura, bloqueio de recursos, dados do PDV e decisões de supervisor/comerciais pertencem aos hooks. O SDK verifica a conclusão, sem simular dispositivos ou aprovações.
- Gramáticas insuficientes nos PDFs exigem `resolve_field`/`resolve_application_data`; dados NFPAG não inferíveis precisam de PaymentMethod.data ou payment_data. Delimitadores sem escape documentado são recusados.
- Catálogo integral não comprova execução de todos os produtos, bandeiras ou autorizadores. Os testes próprios exercitam formatos, cancelamento, recibos, múltiplos pagamentos, escopos, crash, recuperação e IPC/ABI com uma biblioteca C própria.
- Nenhum pagamento real, pinpad físico, impressão física ou homologação de produto foi executado por esses testes. Smoke do simulador fornecedor limita-se a load/configuração/capacidades/versão.
- Persistem lacunas nativas explícitas em rotinas diretas sem header/limite de saída verificado, callbacks nativos e variantes ASCII. `capabilities()` informa presença e bloqueios; não existe FFI arbitrária.
- Parâmetros adicionais continuam abertos a extensões documentadas. A obtenção de manuais complementares e homologação com fornecedor/autorizadores permanece necessária.

Referências: VRS-258 e Carteiras Digitais1.43 do kitx86 auditado. PDFs do kitx64 não foram usados.
