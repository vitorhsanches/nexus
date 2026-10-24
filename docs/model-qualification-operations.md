# Model qualification operations

Model qualification is separate from model activation and project cost authorization.
Newly qualified models are inactive. The operator must activate one explicitly in
**Configurações → Modelos**, and paid-model use still requires its independent
project/model cost authorization.

Administrative writes require both a loopback connection and a bearer token. Set
`NEXUS_ADMIN_TOKEN` in the Nexus server process environment to a random secret of
at least 32 characters before starting the server. The cockpit accepts the token
for the current browser page only; it is held in memory and is not saved or
returned by the API. Rotate it by changing the server environment and restarting.

For PowerShell, generate a value locally without printing it into the repository:

```powershell
$bytes = [byte[]]::new(32)
[System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$env:NEXUS_ADMIN_TOKEN = [Convert]::ToBase64String($bytes)
```

The qualification check compares the current OmniRoute catalog with Codex
App-Server `model/list` metadata and supported reasoning efforts. It does not
perform inference and therefore records protocol-metadata compatibility, not a
successful model completion. Models absent from either exact-ID catalog or
without supported-effort metadata are rejected. Do not treat catalog presence
as proof of runtime inference compatibility.
