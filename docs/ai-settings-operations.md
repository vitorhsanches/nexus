# Configurações de IA do cockpit

O botão de engrenagem abre **Configurações → IA** para o projeto autorizado. O operador pode salvar uma preferência de ferramenta, modelo e esforço para Maestro, desenvolvimento e revisão. As preferências ficam no SQLite do Nexus, por projeto e função. Elas não qualificam ou ativam modelos, não concedem autorização de custo, não habilitam Development Execution e não aprovam uma missão. Sem preferência salva, os fluxos anteriores continuam.

## Codex e OmniRoute

O Nexus procura o executável Codex pelo mesmo local usado pelo dispatcher (`%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe`). O perfil é `NEXUS_OMNIROUTE_CODEX_HOME\config.toml`, ou `%USERPROFILE%\.codex-omniroute-worker\config.toml` por padrão, e passa pela validação de segurança já usada para iniciar o App-Server. A autenticação e a conexão pertencem a esse perfil: o catálogo `model/list` confirma metadados do App-Server e a lista OmniRoute informa as identidades selecionáveis. Isso não prova que uma inferência real terá sucesso.

Um modelo só aparece para uma função quando sua identidade e provedor foram descobertos, o App-Server anuncia o esforço, a qualificação cobre a capacidade, a ativação está vigente e, se pago, existe uma autorização ativa do projeto com uso e concorrência restantes. O backend repete essas verificações ao salvar e, para a preferência do Maestro, antes de cada nova mensagem. Um modelo de planejamento exclusivo não é oferecido para desenvolvimento ou revisão. O Maestro mantém sua autorização própria antes de iniciar a sessão e os limites de custo são consumidos no gate existente.

A preferência de desenvolvimento fornece o modelo inicial quando um novo pane manual é criado sem modelo explícito. A escolha é revalidada antes da criação; iniciar turnos e missões continua sujeito aos gates atuais. O esforço salvo ainda não é transmitido pelo executor de desenvolvimento, que usa seu próprio contrato App-Server. A preferência de revisão é um registro para seleção do operador; o ManagerReview global ainda recebe seu modelo pelo roteamento próprio, sem contexto de projeto para aplicar esta preferência com segurança. A interface não afirma que uma preferência salva iniciou uma execução.

## Claude Code

O Nexus verifica se `claude` aparece no `PATH`, mas não possui um adaptador nativo de Claude Code. Mesmo que a CLI esteja instalada, a tela mostra **Não conectado** e impede salvar essa ferramenta. Um adaptador real precisa verificar autenticação e capacidades da CLI, mapear modelo e esforço, impor sandbox e aprovações, transmitir eventos, tratar cancelamento/reconexão e passar pelos mesmos gates de risco e custos. A presença de um modelo Claude no OmniRoute não equivale à conexão da CLI Claude Code: essa rota pode ser executada pelo Codex App-Server, se qualificada e autorizada.

## Controle administrativo

Gravar ou limpar preferências requer conexão loopback e `Authorization: Bearer <NEXUS_ADMIN_TOKEN>` validado pelo backend. O botão de desbloqueio verifica o token por uma chamada protegida. O token fica apenas em memória na aba e não é salvo. A leitura não retorna credenciais. O catálogo completo e os controles anteriores continuam em **Configurações avançadas · catálogo OmniRoute**.
