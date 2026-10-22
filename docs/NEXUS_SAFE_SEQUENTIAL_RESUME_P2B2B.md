# P2B.2B — retomada sequencial após handoff

## Contrato operacional

O coordenador P2B.1 continua sendo o único controlador da sequência. Se a
missão de um agente deixa alterações para uma dependência no worktree seguinte,
a execução persistida fica `BLOCKED` com
`DEPENDENCY_WORKTREE_NOT_TRANSFERRED`. Preview, aprovação e transferência
P2B.2A continuam ações separadas. Nenhuma delas inicia uma missão.

`GET /api/workspace/plans/{plan_id}/execution` é somente leitura. O campo
`resume` informa elegibilidade, etapa concluída, etapa pendente, handoff e
motivo de recusa. `POST /api/workspace/plans/{plan_id}/resume` é a ação
explícita do operador. Ela só aceita esse bloqueio específico, com a etapa
pendente ainda sem `mission_id` nem `started_at`. Retorna a mesma execução,
agora reservada ou em andamento. Chamadas duplicadas recebem conflito.

Antes do claim e imediatamente antes de lançar a etapa, o backend verifica:

- Plano ainda aprovado, ordem, modelos por ID exato, custo confirmado para
  cada rota não gratuita, perfis de teste e limites persistidos.
- Todas as etapas anteriores `READY_FOR_REVIEW` com `mission_id` persistido;
  etapas posteriores intactas. A missão da dependência é a missão da etapa,
  nunca simplesmente a mais recente da sessão.
- Handoff `TRANSFERRED` com projeto, plano, agentes, panes, sessões, bases Git,
  missão, fingerprint, aprovação, resultado verificado e ausência de estado
  incerto correspondentes.
- SHA-256 de cada arquivo, diff completo no destino, HEAD e review da origem
  correspondentes ao snapshot aprovado. Alteração posterior no destinatário
  impede a retomada.

O claim usa `BEGIN IMMEDIATE`, preserva a execução original, gera novo token
de ownership, muda somente a etapa pendente de `BLOCKED` para `PENDING`,
registra a tentativa em `resume_history_json` e persiste `RUNNING` antes de
iniciar o worker. A criação de missão e dos turnos internos exige o token
vigente, conferido na transação SQLite. Chamadas manuais aos endpoints do
Workspace ou Development Mode não conseguem lançar etapas da sequência.

Após um crash entre claim e lançamento, a recuperação P2B.1 pode assumir o
lease vencido e lançar uma etapa comprovadamente `PENDING` uma vez. Em
`STARTING` sem `mission_id`, o resultado fica incerto e não há replay. Com
`mission_id`, o coordenador observa a missão existente. O token anterior
perde o direito de atualizar estados ou criar novas missões.

## Topologia e limites

Esta versão aceita uma dependência declarada no agente imediatamente anterior:
`A → B` e a cadeia `A → B → C`, com handoff distinto em cada transição que
contenha alterações. Múltiplas dependências de escrita para o mesmo destino
continuam bloqueadas. O worktree destinatário precisa satisfazer as regras
de transferência P2B.2A; não há combinação de worktrees modificados.

`FAILED`, `INTERRUPTED`, `RESULT_UNCERTAIN`, erros de integridade, falhas de
autorização, lançamentos prévios e bloqueios não ligados ao handoff não podem
usar este resume. Um handoff `RESULT_UNCERTAIN` segue exigindo inspeção
humana; não há botão para declará-lo concluído.

Ao terminar, a execução fica `AWAITING_REVIEW`. O Nexus não aplica mudanças
ao projeto original, não faz commit, push ou merge e não dispensa a revisão
humana de integração. A verificação de arquivos detecta deriva observável,
mas um escritor externo que ignore os gates SQLite pode competir com a
janela entre verificação e lançamento; o ambiente local controlado continua
sendo uma premissa operacional.
