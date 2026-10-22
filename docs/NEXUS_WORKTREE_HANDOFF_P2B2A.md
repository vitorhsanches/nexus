# P2B.2A — transferência segura entre worktrees

O handoff copia o conteúdo de arquivos de texto de uma missão `READY_FOR_REVIEW`
para o worktree isolado de um agente dependente. Ele não inicia uma missão,
não aplica alterações ao projeto original e não faz operações de integração Git.

## Contrato

1. `POST /api/workspace/handoffs/preview` identifica os dois panes e a missão.
   O serviço verifica projeto, plano, dependência declarada, sessões, identidade
   dos worktrees, bases Git idênticas e destino limpo. A lista de arquivos e o
   `review_id` devem coincidir com a evidência salva quando a missão terminou.
   O preview registra `PREVIEWED` no SQLite, sem escrever em qualquer worktree.
2. `POST /api/workspace/handoffs/{id}/approve` exige o fingerprint e ambos os
   IDs de pane. O servidor recalcula o snapshot antes de gravar `APPROVED`.
3. `POST /api/workspace/handoffs/{id}/transfer` recalcula o snapshot, reserva o
   destino com `BEGIN IMMEDIATE` e grava `TRANSFERRING` antes da primeira
   escrita. Inícios de missões e turnos diretos consultam essa barreira na
   própria transação de reserva. Cada arquivo é verificado por SHA-256 após a
   cópia. Somente então o registro se torna `TRANSFERRED`.
4. `GET /api/workspace/handoffs/{id}` e
   `GET /api/workspace/plans/{plan_id}/handoffs` expõem estado e histórico.

O registro inclui identidades de projeto, plano, panes, sessões e missão, os
SHAs das bases e HEADs, fingerprint, caminhos, hashes, status, tempos, token
de ownership e resultado. A aprovação só vale para aquele snapshot. Um erro
antes da intenção bloqueia o registro; após a intenção, o resultado é
`RESULT_UNCERTAIN`. Um processo morto no mesmo host é detectado no startup e
o registro fica incerto sem repetir a escrita. Um proprietário ainda vivo ou
de outro host permanece `TRANSFERRING` e bloqueia o destino.

## Escopo e limites

São aceitos somente arquivos regulares UTF-8, adicionados ou modificados,
dentro do modo de arquivo comum `100644`,
dentro dos limites do diff existente (80 arquivos, 128 KiB por arquivo e
1 MiB de patches). Arquivos binários, omitidos, truncados, sensíveis,
symlinks, hardlinks, exclusões, renomeações, submódulos, caminhos protegidos,
conflitos de destino e bases divergentes são recusados. O destino precisa
estar inteiramente limpo antes da cópia; esta versão não combina múltiplos
handoffs em um mesmo worktree já modificado.

O SQLite serializa tentativas entre processos e o token impede que um dono
antigo registre sucesso. Um `TRANSFERRING` ou `RESULT_UNCERTAIN` bloqueia
novas transferências e execução no destino até análise humana. A substituição
de cada arquivo modificado é atômica; uma falha entre arquivos pode deixar
alguns copiados e por isso não é tratada como sucesso nem sofre rollback
destrutivo. Um processo externo que escreva diretamente no filesystem fora
dos gates do Nexus não participa da transação SQLite; a revalidação e os
hashes detectam deriva observável, mas a operação pressupõe o ambiente
local controlado previsto nesta fase.

O coordenador continua `BLOCKED` em
`DEPENDENCY_WORKTREE_NOT_TRANSFERRED`; a existência de preview, aprovação ou
transferência não retoma a sequência. O registro `TRANSFERRED` é a evidência
persistida que a P2B.2B poderá validar antes de implementar a retomada.
