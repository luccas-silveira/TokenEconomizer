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

- `discover(host, env, dirs)`: UMA varredura de `host.history(env, dirs)`, lendo a cabeça de
  cada log (`host.log_head`) uma vez, e agrupa por repo com `history._identity` (cache de
  `repo_identity`). Log cujo `cwd` não existe mais vai para o grupo do repo vivo cuja pasta do
  Claude Code bate exatamente com a do log (a mesma regra de `history.is_this_repository`); sem
  repo vivo, fica de fora (`skipped["gone"]`). Para cada grupo devolve `{key, label, cwd, logs}`,
  com `cwd` existente e a lista de logs já com cabeça lida.
- `history.plan` ganha um parâmetro opcional `logs` (a lista do grupo, com cabeça): com ele,
  não varre o histórico de novo. Sem ele, comportamento atual.
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
- Log do processo em `<state_root>/history-all.log` (não no store do repo que disparou).
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
- Custo: os tokens do proposer entram no registro do ledger da sessão importada, como overhead
  (já acontece em `fold.py:574-576`). `accounting.summarize` hoje descarta registros importados
  antes de somar overhead (`accounting.py:41-43`): ganha uma soma separada
  (`imported_overhead`), e `te status` mostra uma linha "history: N session(s) read, X tokens
  spent reading them" quando for maior que zero.
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

## Decisões do grill

- **A1** — micro-skills continua desligado por padrão no código; na instalação desta máquina
  roda `config set microskills=on --global`. Motivo: o usuário quer o fork perto do original
  para um PR possível.
- **A2** — descartado. A retenção de 30 dias só limita o que existe hoje; depois de instalado,
  o plugin aprende cada sessão ao terminar e não depende mais do histórico.
- **A3** — confirma o disparo no primeiro `SessionStart`.
- **A4** — soma separada do gasto importado em `accounting` e linha em `te status`. Motivo:
  sem isso o custo prometido pela spec fica invisível.
- **A5** — `discover` faz uma varredura só e passa os logs ao `plan` de cada repo. Motivo:
  `plan` por repo leria as 1.074 cabeças umas 60 vezes.
- **A6** — log de pasta sumida rota pelo nome da pasta do Claude Code, como no import por repo.
  Motivo: não perder sessões de checkout movido.
- **A7** — mantido: montar o contexto de cada repo faz a mesma adoção que a primeira sessão
  faria; erro vira linha no log e o run segue.
- **A8** — log do run global em `<state_root>/history-all.log`.
- **A9, A10, A12** — confirmam a spec.
- **A11** — testes que mudam: `test_history_import.py:204-220` (padrão) e `:608-624` (passa a
  esperar chamadas só na cauda).
