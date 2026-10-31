# Estado da implementação

Este pacote é um cliente asyncio da ponte Rust compartilhada com o projeto Node. A produção está limitada a Windows x64. O pacote Python não contém biblioteca do fornecedor, PDF, INI, chaves ou dados de operação.

## Implementado e testado com fontes próprias

- Mapeamento explícito de configuração, início, continuidade e finalização para o contrato camelCase. Parâmetros adicionais permanecem strings integrais.
- Factory `create_sitef`, com carregamento, validação do protocolo/ABI, cleanup de falha e chave SHA256 de journal por loja/terminal igual à dos clientes Node/PHP.
- Subprocesso sem shell, serialização de chamadas, limites de frame, UTF-8 estrito, correspondência de ID e falhas fatais sem restart ou retry automático.
- Callback assíncrono, cancelamento cooperativo, echo exato de bytes no comando zero, campos desconhecidos e repetidos preservados em sequência.
- Journal exclusivo, decisões duráveis e retomada com identidade, decisão e escopo preservados; finalização explicitamente local/void.
- Política `requires_finalization` explícita por operação, incluindo consultas e funções auxiliares interativas.
- Catálogo JSON comum empacotado como dado, sem manter outra cópia manual das tabelas Python.
- Testes unittest de processo real sintético e teste opcional com a ponte Rust + biblioteca C de contrato própria.

## Cobertura que continua incompleta

Os 25 códigos de retorno explícitos e as duas regras gerais da VRS-258 seção 5.2.1 estão no catálogo comum, além de dois códigos do suplemento de carteiras. Os 124 IDs da seção 5.2.2 estão nomeados, com sete IDs adicionais de carteiras. Nomear um ID e permitir enviá-lo não implementa o fluxo de sua modalidade.

Na seção 5.3.2, o documento tem 444 códigos individuais e três intervalos, representados em duas linhas agrupadas. O catálogo atual tem 64 aliases de campo, dos quais 40 pertencem a essas 444 entradas. Faltam 404 aliases/descrições individuais dessa tabela e muitas regras de formato e produto. Não há promessa de semântica completa dos 444 campos.

Continuam dependentes de implementação da aplicação: impressão imediata de comprovantes administrativos/Corban; agrupamento e validação dos resultados Corban; NFPAG completo; formatos anunciados no campo 2355; políticas de parcelas e valores por produto; interpretação contextual de campos que compartilham números; ações operacionais de erros como sangria. O transporte bruto e os callbacks permitem desenvolver esses tratamentos, sem substituí-los.

O helper tem lacunas explícitas nas rotinas diretas com callbacks nativos e variantes ASCII. Sua disponibilidade é consultada por `capabilities()`. O pacote Python não oferece um método arbitrário para chamar símbolos desconhecidos da DLL.

O helper JSON é a autoridade dos métodos auxiliares. O Python fornece os modelos das chamadas principais; em `call_native` os parâmetros usam o contrato camelCase da ponte. A classificação Python de retorno especial permanece bruta em contagens, presença/teclas/confirmações de pinpad e outros domínios específicos; consulte a especificação da rotina em vez de aplicar a regra de transação.

## Itens para uma versão homologada

1. Resolver os 444 campos individualmente, com formato, contexto, dados sensíveis e obrigações verificáveis. Corrigir qualquer conflito de significado em ranges dependentes da modalidade.
2. Implementar e testar os fluxos de cada produto realmente contratado, inclusive impressão, múltiplos pagamentos, estorno, falha de energia e recuperação.
3. Confirmar os headers Windows x64 das rotinas diretas ainda pendentes, incluindo cancelamento e limites de saída.
4. Completar o catálogo de parâmetros a partir da documentação complementar oficial; o pass-through continuará aberto para extensões.
5. Validar o frontend integrado ao modal compartilhado ou o frontend próprio, com teclado, cancelamento, formatos e exibição segura de dados.
6. Executar homologação com fornecedor, autorizadores e pinpad. Os testes sintéticos não são homologação financeira.

Referência documental: VRS-258 e Carteiras Digitais 1.43 do kit x86 previamente auditado. PDFs do pacote x64 não foram usados. As bibliotecas de runtime são fornecidas separadamente pela aplicação integradora.
