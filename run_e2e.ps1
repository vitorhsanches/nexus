Set-Location $PSScriptRoot
$mission = @'
Analise a robustez operacional do fluxo GO do Nexus após a implementação da integração de workers aprovados. Investigue de forma abrangente a execução concorrente, propagação entre workers dependentes, integração de worktrees, estados de lifecycle e tratamento de falhas. Identifique problemas reais e de baixo risco que possam impedir uma missão multi-agent de concluir corretamente. Implemente somente as correções mínimas comprovadamente necessárias, adicione ou ajuste os testes relevantes e valide que o fluxo continua preservando concorrência, dependências, segurança das alterações locais do usuário e integração final no repositório principal.
'@
python -m nexus go --project nexus $mission 2>&1 | Tee-Object -FilePath (Join-Path $PSScriptRoot 'e2e_output.txt')
