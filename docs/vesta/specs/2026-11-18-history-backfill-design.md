# Aprender com o histórico do Claude Code desde a instalação

## Objetivo

Ao instalar o TokenEconomizer no Claude Code, o plugin aprende primeiro com as sessões que já
estão em `~/.claude/projects` (todas as pastas, não só a atual) e depois continua aprendendo com
as sessões novas, como já faz.

Sucesso: na primeira sessão nova num repo que tinha histórico, os fatos aprendidos daquele
histórico (inclusive os propostos pelo modelo) já podem entrar no início da sessão, sem o usuário
rodar comando nenhum depois de instalar.

## O que já existe (não muda)

`te history import` (`tokeneconomizer/plugin/history.py`) importa o histórico de UM repo, na
primeira sessão nele, quando `history_import` está ligado. Garantias que ficam como estão:
ordem de tempo, só entra num store sem nada aprendido ao vivo, trava de fold durante o run,
orçamento de 30 min com retomada, heartbeat, sessões que terminam durante o import esperam e
são aprendidas depois, sessão importada nunca segura deny list.

## Mudanças

### 1. `history_import` ligado por padrão

`Config.history_import` passa a `True`. O alias de ambiente `TE_HISTORY_IMPORT` e
`config set history_import=off` continuam desligando.

### 2. Import de todas as pastas: `te history import --all`

Novo módulo `tokeneconomizer/plugin/history_all.py`:

- `discover(host, env, dirs)`: percorre `host.history(env, dirs)`, lê a cabeça de cada log
  (`host.log_head`) para o `cwd` registrado, e agrupa por `repo_identity(cwd).key`. Para cada
  grupo devolve `{key, label, cwd, sessions}` com um `cwd` que existe no disco. Grupo sem nenhum
  `cwd` existente fica de fora (`skipped["gone"]`).
- Ordem: o repo da sessão que disparou primeiro, depois por número de sessões, decrescente;
  empate por `key`.
- `run_all(host, env, first_cwd=None, budget_s)`: para cada grupo, monta
  `LiveContext.build(host, cwd, env)` e chama `history.run(ctx, auto=True)` sem mudar sua
  lógica. Repo que já tem `history.json` em estado final (`done`, `skipped`, `stopped`) é
  pulado. Um `partial` (orçamento de 30 min estourado) conta como pendente e o próximo run
  global retoma ele.
- Estado global em `<state_root>/history-all.json`:
  `{"state": "running"|"partial"|"done", "queue": [keys], "next", "done": [keys],
  "skipped": {why: n}, "started", "heartbeat", "finished", "pid"}`. Heartbeat parado por
  `HEARTBEAT_STALE_S` deixa a próxima sessão reiniciar. Gravação atômica com trava, como
  `history.json`.
- `--dry-run`: imprime a fila (label, sessões, pasta) e o que fica de fora, sem gravar nada.

`history.run` com `auto=True` precisa que `history.json` tenha sido marcado antes (é o que
`maybe_start` faz). `run_all` faz a mesma marcação por repo antes de chamar `run`, pelo
mesmo `_update`, para reaproveitar a recusa "store com sessões aprendidas" e o heartbeat.

### 3. Disparo na primeira sessão depois de instalar

No `SessionStart` do Claude Code, ao lado do `history.maybe_start` existente:
`history_all.maybe_start(ctx)` lê `history-all.json`; se não existe (e `history_import` está
ligado), ou está `running` com heartbeat parado, ou `partial`, dispara em segundo plano
`te history import --all --auto` via `ctx.spawn`, passando o `cwd` atual como primeiro da fila.
Enquanto o run global está vivo, o `history.maybe_start` por repo não dispara um segundo
processo para um repo que está na fila global (evita dois imports no mesmo repo; a trava de
fold já impede a corrida, isto evita o processo à toa).

Repo novo, sem histórico antes da instalação, segue o caminho por repo que já existe.

### 4. O modelo que propõe fatos passa pelas sessões recentes do histórico

Hoje `fold._fold_microskills` nunca dá `runner` a uma sessão importada. Muda: a sessão importada
recebe o `runner` do proposer quando está entre as `history_propose_sessions` mais recentes do
plano daquele repo. O ritmo é o de sempre (`microskills_propose_every`, padrão 3), contado pelo
distiller.

- Nova configuração `history_propose_sessions` (padrão `12`, `0` = sem modelo no histórico,
  comportamento antigo). Ambiente: `TE_HISTORY_PROPOSE_SESSIONS`.
- `history._import_one` calcula se o item está na cauda do plano e passa `propose=True` para
  `fold.fold_locked(..., imported=True, propose=...)`, que repassa para `_fold_microskills`.
- Só com `microskills` num perfil pago (`icfg.paid`) e o sidecall não pausado; senão, cai no
  comportamento atual (só a camada gratuita).
- Custo: os tokens do proposer entram no registro do ledger da sessão importada, como overhead,
  e `te status` mostra o gasto do histórico separado.
- As chamadas rodam em série, dentro do processo em segundo plano. As sessões do próprio
  proposer (`claude -p`) já ficam de fora do histórico pela regra `headless`.

## Erros

Regra do projeto: qualquer exceção em hook vira `{}` e uma linha em `errors.log`. Uma falha num
repo do run global é registrada e o run segue para o próximo. Chamada do proposer que falha pula
o tick, como já acontece no distiller.

## Documentação

`README.md` (seção que fala de `history_import`), `docs/PLUGIN.md` (parágrafo "Earlier
sessions" e a linha do `SessionStart`), `plugin/README.md` (Settings e Earlier sessions),
docstrings de `history.py` e `config.py`. Sem alegação de efeito no texto do plugin.

## Testes

- Unitários em `tests/`, no padrão de `tests/test_history*.py`: agrupamento por repo, pasta
  apagada fora, ordem da fila, retomada do global, repo já aprendido pulado, proposer chamado só
  na cauda (runner falso contando chamadas), `history_propose_sessions=0` sem chamada, default
  `history_import=True`.
- `python -m pytest tests experiments/tests` verde.
- Real, no histórico desta máquina: `te history import --all --dry-run`, depois o import de
  verdade com o modelo ligado em 2 ou 3 repos, conferindo `te status` (sessões lidas, custo) e
  que `injection.json` tem fatos ativos.

## Fora de escopo

Codex, Copilot e os outros agentes (o pedido é o `~/.claude`). Mudar as regras de confirmação
dos fatos. Deny list de ferramentas aplicada sozinha a partir do histórico (continua esperando
`toolprune apply`).
