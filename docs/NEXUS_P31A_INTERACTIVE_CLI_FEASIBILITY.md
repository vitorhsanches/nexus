# P3.1A — CLI interativa por pane: impedimento e menor adaptação segura

Base auditada: `sync/portable-nexus-snapshot` em
`298f5d3a82f83711a914f79fdfaf72d953b125b7`.

## Resultado da auditoria

A prova solicitada, em que o pane é **a mesma sessão Codex interativa** usada
pelo agente e pelo Maestro, não é segura com os contratos atuais. Nenhum
terminal foi apresentado ao usuário como funcional nesta etapa.

`nexus.chat.appserver_client.AppServerClient` transporta JSON-RPC sobre os
pipes stdin/stdout de `codex app-server`. `nexus.dev.executor` cria um cliente
por turno, retoma o `codex_thread_id` persistido, envia `turn/start`, transmite
notificações, permite `turn/interrupt` e encerra o cliente ao fim do turno.
O processo está vinculado ao worktree e ao `codex_home` da sessão Development.
Esses pipes são o protocolo JSON-RPC, não um PTY para bytes de teclado.

O Codex CLI instalado (`0.163.0-alpha.10.2`) expõe no schema gerado de seu
App-Server `command/exec` com `tty`, `processId`, saída em
`command/exec/outputDelta`, `command/exec/write`, `command/exec/resize` e
`command/exec/terminate`. Essa é uma capacidade real de PTY e evita uma
implementação ConPTY paralela. Porém, o próprio contrato define
`command/exec` como **comando avulso, sem criar thread ou turn**. Uma shell
nesse PTY não é a CLI Codex do agente. Abrir `codex` dentro dela iniciaria
outro cliente/processo Codex, sem vínculo comprovado com o
`codex_thread_id` do pane e podendo concorrer com a sessão do Maestro.

O registro de turnos e missões, as barreiras de handoff e o fencing da
execução coordenada protegem os caminhos Development existentes. Não há
hoje uma reserva durável de posse para um terminal manual de longa duração.
Ligar `command/exec` diretamente ao browser contornaria esses gates.
`thread/shellCommand` também não serve como atalho: a documentação do
App-Server informa que executa fora do sandbox do thread. O experimental
`process/spawn` igualmente inicia processo fora do sandbox Codex.

Neste computador, o perfil `~/.codex-omniroute-worker/config.toml` não está
presente. Portanto não houve validação manual de uma sessão Codex real nem
seleção de modelo; nenhuma inferência ou rota paga foi acionada.

## Menor adaptação segura antes de expor o terminal

1. Definir **um único dono de execução por sessão Development** no SQLite,
   com claim transacional e fencing verificável. Turnos manuais, missões,
   coordenador e handoffs devem rejeitar uma sessão reservada por CLI; o
   terminal deve rejeitar sessão ocupada por qualquer um deles. Um simples
   lock em memória ou flag de frontend não basta entre processos.
2. Provar, em fixture Windows com Codex CLI simulado, que a entrada interativa
   da CLI pode retomar **o mesmo** `codex_thread_id` e `codex_home` do pane
   depois que o App-Server liberar a sessão. Persistir o resultado antes de
   devolver a posse ao coordenador. Se o CLI não puder retomar a identidade
   com segurança, manter o pane no modo de turnos App-Server já existente.
3. Só após essa prova, adicionar métodos PTY `command/exec` ao
   `AppServerClient` existente e manter seu processo vivo enquanto o pane
   detiver a reserva. Fixar comando, cwd canônico do worktree, ambiente
   isolado, sandbox `workspaceWrite`, limite de duração e de saída. Não
   oferecer shell arbitrário como substituto do agente Codex.
4. Transportar bytes de teclado, saída e resize por canal local autenticado
   que verifique origem, projeto, sessão, pane e token de posse em cada
   operação. Não expor o App-Server diretamente na rede. No frontend,
   xterm.js renderizaria o PTY; interrupção terminaria o processo associado
   ao `processId` e liberaria o lease apenas após observar o fim.
5. Testar colisões entre processos, desconexão do browser, restart,
   interrupção, handoff e retomada coordenada. Falha com resultado incerto
   deve bloquear novo lançamento até reconciliação; nenhuma ação do pane
   pode aplicar mudanças ao projeto original automaticamente.

## Critério para retomar a implementação

Uma prova automatizada com Codex CLI simulado deve demonstrar entrada,
saída, resize e interrupção num PTY Windows; continuidade da identidade
`codex_thread_id`; e exclusão mútua durável com missões e coordenação. Uma
validação manual posterior exige perfil OmniRoute local válido e um modelo
gratuito explicitamente aprovado. xterm.js sozinho não atende esses critérios.

Fonte do contrato: [documentação oficial do Codex App-Server](https://learn.chatgpt.com/docs/app-server),
seções de API overview e command execution; schema gerado localmente por
`codex app-server generate-json-schema` na versão acima.
