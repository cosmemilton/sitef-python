# Convenções do projeto

- API pública Python; bridge Rust próprio e protocolo NDJSON comum com sitef-node.
- Runtime de produção exclusivamente Windows x64. Testes Linux somente com fixtures próprias explícitas.
- Referência documental exclusivamente PDFs do kit x86 mais recente; não usar PDFs do kit x64.
- DLL, INI, PDF, chaves, certificados e traces do fornecedor nunca acompanham o componente.
- Bibliotecas oficiais e seus caminhos absolutos são fornecidos pelo integrador.
- Nunca usar CmAlert ou similares para notificações; usar CmToast/CmToastNotice no frontend.
- No modal opcional, cosmemilton-ui é obrigatória. Reutilizar componentes/tokens, preservar teclado e leitura em claro/escuro e celular.
- Preservar retornos/campos/parâmetros desconhecidos. Catálogo não equivale a tratamento semântico completo.
- Nunca repetir operações financeiras automaticamente após timeout/crash; finalização é decisão fiscal explícita.
- Não logar PIN, PAN, trilhas, chaves, campos livres ou recibos.
- Manter lacunas documentadas. Testes sintéticos não equivalem a homologação.
- Fontes comuns identificadas em SHARED-SOURCE.json; atualizar a partir do commit canônico em conjunto.
- Validação do cliente: python -m unittest discover -s tests -v.
