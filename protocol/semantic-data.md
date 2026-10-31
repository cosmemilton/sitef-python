# Dados semânticos compartilhados

`fields.json` e `functions.json` são dados próprios de integração, com descrições curtas parafraseadas a partir da referência x86 VRS-258 e Carteiras Digitais 1.43. Não contêm PDFs, código ou bibliotecas do fornecedor. A arquitetura do runtime continua Windows x64.

Os schemas são abertos para metadados adicionais. Os IDs são chaves de string para preservar números negativos e identificadores sem depender de precisão numérica do consumidor. Valores de operação continuam sendo strings; metadados não autorizam conversão para ponto flutuante.

## Cobertura e precedência

O catálogo de campos contém todas as 444 entradas individuais da seção 5.3.2, as três faixas dessa seção, os 64 eventos da seção 5.3.3 e 28 campos adicionais das carteiras ou da consulta de pendências. O catálogo de funções contém os 124 IDs da tabela geral e sete IDs adicionais de carteiras. Isso é cobertura dos dados consultados, com obrigações e pontos de resolução explícitos; não é homologação dos produtos.

`contexts: ["transaction"]` identifica o contexto geral. A leitura segura usa `secure-card-read`. Dentro do mesmo contexto, a entrada individual tem precedência sobre uma faixa genérica. As faixas protegidas 202x/203x/204x/205x só se aplicam à leitura segura. Por exemplo, 2053–2056 têm significados específicos de transação fora da leitura segura; não são hashes em todo fluxo.

Quando o documento não fixa escala, separador, enumeração ou estrutura completa, o metadado diz `resolverRequired`. O engine deve preservar o valor bruto e chamar a aplicação antes de interpretar ou fazer aritmética; um campo cujo significado não é usado pode continuar armazenado como texto. Isso evita introduzir regras não documentadas.

`format.kind` e `action.kind` são identificadores de roteamento. `action.requiredHook` nomeia a obrigação, e `phase` informa o momento/condição. As funções com política `explicit` exigem que a aplicação declare o efeito e a confirmação antes do início. `financial` nunca significa confirmar automaticamente. `receiptPolicy` distingue coleta/impressão imediata da guarda para impressão antes da decisão fiscal.

## Regras relevantes

- 2355 anuncia o formato da coleta seguinte: A, AN, LN, N, Vx, S, M ou Mc. Mc exige confirmação da escolha. AN não fornece uma gramática completa no documento; S fora do comando 20 não fornece codificação universal de respostas. O resolver torna essas decisões explícitas.
- 2470 define precisão para coletas; 1121 e 1126 tratam, respectivamente, preço unitário e quantidade de produto. São contextos separados e reiniciados por transação.
- 121/122 preservam todas as vias e sua ordem; 123 categoriza as vias seguintes. Os comprovantes administrativos/Corban são impressos durante o laço. Comprovantes diferidos são entregues a `storeReceipt` e impressos antes da finalização.
- 5042/5043 exigem aquisição/liberação dos recursos do pinpad. 5074 registra obrigação de assinatura em papel antes da confirmação; não significa desenhar ou simular assinatura automaticamente.
- 5010 é notificação de etapa de assinatura. A referência geral não fixa meio ou responsável; o produto pode implementar tratamento próprio sem uma obrigação universal de captura por esse evento.
- 4039 não zero é erro lógico de PBM que precisa ser resolvido antes de confirmar. 4100 é diagnóstico de comunicação a preservar e encaminhar, sem repetição financeira automática.
- Aritmética de 109/137/138 exige escala explícita: o documento não define uma interpretação universal para uma sequência de dígitos desses campos. `monetaryFormats` ou `resolveField` devem fornecer `{unscaled, scale}` exatos.
- 730/731/732 descrevem NFPAG. O requisito 13 restringe as redes do requisito 03, e não é um dado a coletar/enviar. A origem de 03 é explicitamente o campo 131. NFPAG confirma o conjunto do documento/data, com semântica diferente do filtro `NumeroPagamentoCupom`.
- O parâmetro `RedeConfirmacao` não tem uma associação inequívoca 131/158 na referência. Exigir `confirmationNetworkField` ou `resolveConfirmationNetwork` quando os valores não forem ambos iguais; não selecionar silenciosamente o único campo disponível.
- Múltiplos TEFs mantêm grupo por documento/data. Uma virada de dia permite outro documento e exige decisão separada para cada grupo original, conservando suas horas. Falha em uma tentativa não desfaz automaticamente pagamentos anteriores aprovados.
- As condições de preço PBM 1042/1043/1044 são avaliadas por medicamento (1011) quando a escala estiver resolvida. Quantidades/códigos de combustível respeitam 1122/1123 quando a comparação for inequívoca. `validateTransaction` recebe os campos e as obrigações não resolvidas; violações inequívocas impedem confirmação. Demais limites explicitamente informados pelo produto também exigem revisão da aplicação antes de finalizar.
- O anúncio de requisitos 730/731/732 não torna NFPAG universalmente obrigatório. A obrigação de fornecer os dados é aplicada quando NFPAG for usado pela aplicação/produto. A barra invertida é um separador de linhas na interface ASCII A; não é aplicada ao buffer da interface binária.

## Auditoria

A cobertura foi confrontada com nova extração dos PDFs x86, separada da autoria dos metadados. O censo conferiu 444 campos individuais, três faixas, 64 eventos, 36 campos na tabela de carteiras e 124 funções gerais. As contagens por página e os hashes dos arquivos estão em `coverage-audit.json`. Não se usou nenhum PDF do kit x64.
