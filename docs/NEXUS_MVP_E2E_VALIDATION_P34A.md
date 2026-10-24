# P3.4A — Validação end-to-end do MVP

## Contratos auditados

O `README.md` aponta a instalação e operação local para
`docs/SETUP_WINDOWS.md`. A validação seguiu os contratos ativos abaixo, sem
criar um segundo caminho de execução:

| Área | Implementação reutilizada | Garantia observada |
| --- | --- | --- |
| Maestro | `nexus/workspace/maestro.py` e `maestro_plan.py` | planejamento sem autorização usa política determinística, rotulada e sem Chat externo |
| Agente | `nexus/dev/executor.py` e `registry/dev_sessions.py` | um turno por sessão, com estado durável; o limite externo foi substituído por mock |
| Missão | `nexus/dev/mission_runner.py` e `registry/dev_missions.py` | execução limitada, perfil confiável, um reparo, cancelamento e estados terminais |
| Worktree | `nexus/dev/service.py` e `worktree.py` | branch por sessão, identidade Git verificada e alterações fora do checkout original |
| Autorização | `nexus/workspace/service.py` | Development Mode, catálogo, custo e autorização do plano falham fechados |
| Aprovação | `nexus/dev/integration.py` | snapshot persistido para revisão humana; aplicar continua sendo outra ação explícita |

## Escopo validado

O teste direcionado `tests/test_mvp_e2e_validation.py` percorre o contrato do
MVP sem contas, credenciais, rede ou inferência real:

1. o Maestro recebe a tarefa e produz um plano determinístico, persistido e
   explicitamente identificado como sem IA;
2. a aprovação cria uma pane, uma sessão de desenvolvimento e uma worktree Git
   real e isolada;
3. um mock do limite externo do Codex App-Server executa uma alteração
   determinística somente nessa worktree;
4. o mission runner real persiste os estados e executa um perfil de teste
   confiável em subprocesso local;
5. a missão chega a `READY_FOR_REVIEW` e exige revisão humana;
6. o serviço real de integração cria apenas um snapshot durável de revisão;
   ele não chama `apply_review`, não faz commit, merge ou push e não altera o
   checkout de destino.

A fixture usa um repositório Git, raiz de worktrees e banco SQLite exclusivos
de `tmp_path`. O teste também reabre o estado persistido, simula a recuperação
de uma missão órfã como `OUTCOME_UNKNOWN` sem replay e compara HEAD, status e
arquivos do checkout original antes e depois do fluxo.

## Bordas simuladas e garantias

São mockados ou bloqueados por sentinelas:

- Codex App-Server e o turno de inferência;
- catálogo/telemetria OmniRoute;
- criação e envio de sessão do Nexus Chat;
- qualquer modelo ou provedor externo.

O perfil confiável de teste continua real, mas chama apenas o próprio Python
local com um script determinístico. Git, SQLite, Maestro, aprovação, criação e
validação de worktree, gates do workspace, state machine da missão e snapshot
de revisão usam as implementações reais do Nexus.

Os cenários negativos comprovam bloqueio antes de persistir nova missão para:

- projeto sem autorização de Development Mode;
- modelo pago sem consentimento explícito de custo;
- modelo ausente do catálogo aprovado.

Também são validados falha após o único reparo permitido e cancelamento
durável antes dos testes.

## Execução direcionada

No ambiente Python de desenvolvimento do repositório:

```powershell
python -m pytest -q tests/test_mvp_e2e_validation.py
```

Esse é o único conjunto necessário para P3.4A. Ele não instala nem procura
OmniRoute e não depende das variáveis ou credenciais de uma máquina real.

## Limitações desta validação

- `plan_view()` atualmente reutiliza a chave `execution` do plano para o
  estado do coordenador. Antes de existir uma execução coordenada, a visão
  pública retorna `execution: null` e oculta a ordem sequencial presente em
  `proposal_json`; a ordem continua recuperável pela lista persistida de
  agentes. P3.4A apenas registra o problema para não conflitar com o P3.3G,
  que também altera `nexus/workspace/service.py`.
- A validação comprova a composição local dos serviços, não a compatibilidade
  de protocolo com uma versão específica do Codex App-Server.
- Não mede latência, custo, rate limits, qualidade de modelo ou disponibilidade
  de provedor.
- Não cobre autenticação real, streaming SSE vindo de um processo externo ou
  a confirmação do provider retornada pelo App-Server.
- Não aplica a revisão de propósito. O passo destrutivo continua separado e
  depende de ação humana explícita.
- A retomada validada é conservadora: após perda do processo, uma missão ativa
  vira `OUTCOME_UNKNOWN`, preserva a worktree e não é repetida automaticamente.

## Requisitos para uma futura execução real

Em um computador destinado à validação real, configurar separadamente:

- Codex CLI/App-Server compatível e um `CODEX_HOME` isolado;
- OmniRoute instalado e acessível pelo perfil exigido pelo Nexus;
- credenciais e catálogo de modelos válidos, com um modelo explicitamente
  aprovado;
- autorização de custo com limites de uso quando a rota não for gratuita;
- `NEXUS_DEV_EXECUTION_ENABLED=1`, projeto habilitado para Development Mode e
  um perfil de testes confiável;
- repositório descartável ou branch/worktree exclusiva, sem alterações locais;
- operador disponível para revisar o diff e decidir separadamente se deseja
  chamar a integração.

Antes dessa execução, rode novamente este teste offline. Depois, use um teste
de diagnóstico explicitamente opt-in; nunca substitua os mocks desta suíte por
uma conexão implícita ao OmniRoute.
