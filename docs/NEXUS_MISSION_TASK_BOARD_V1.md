1. Conceitos
RUN

Uma solicitação completa do usuário.

Exemplo:

RUN-001

"Implementar integração Google Calendar"
MISSION

O objetivo operacional derivado pelo Manager.

Exemplo:

MISSION-001

Adicionar suporte completo ao Google Calendar
TASK

Uma unidade executável.

Exemplo:

TASK-001
Criar OAuth service

TASK-002
Criar Calendar API client

TASK-003
Criar testes
ATTEMPT

Uma tentativa de execução.

Exemplo:

TASK-001

Attempt 1
Agent: Claude Sonnet
Status: FAILED

Attempt 2
Agent: GPT
Status: COMPLETED
REVIEW

Validação.

Estados:

PENDING
PASS
RETRY
BLOCKED
2. Lifecycle

Vamos definir:

CREATED
   |
   v
READY
   |
   v
CLAIMED
   |
   v
RUNNING
   |
   +----------+
   |          |
   v          v
 REVIEW     FAILED
   |
   +------+
          |
          v
      COMPLETED
3. Task Contract

Modelo inicial:

{
  "task_id": "TASK-001",
  "run_id": "RUN-001",
  "title": "",
  "description": "",
  "status": "READY",
  "priority": "MEDIUM",
  "dependencies": [],
  "assigned_agent": null,
  "execution_policy": {
    "preferred_model": "",
    "fallback_models": []
  },
  "acceptance_criteria": []
}
4. Board Views

A interface futura terá:

BACKLOG

READY

IN PROGRESS

REVIEW

DONE

BLOCKED
5. Regras importantes

Vamos documentar:

Task é a unidade de trabalho.
Agent executa Task, não possui Task.
Retry cria novo Attempt.
Histórico nunca é sobrescrito.
Board é uma visualização do estado real.
Não haverá lógica importante somente no frontend.