# Plano — aprender com todo o histórico do Claude Code desde a instalação

Objetivo: na primeira sessão depois de instalar, o plugin lê em segundo plano as sessões de todas
as pastas em `~/.claude/projects`, com o modelo de fatos nas 12 mais recentes de cada repo, e
depois segue aprendendo com as sessões novas.

Spec: `docs/vesta/specs/2026-11-18-history-backfill-design.md` (com `## Decisões do grill`).

Comando de teste: `python3 -m pytest tests`
(`experiments/tests` tem 5 falhas anteriores a este trabalho, em `test_model_spelling.py`,
ligadas ao Copilot CLI desta máquina; ficam fora da prova.)

Pastas de tela: nenhuma. Mockup: nenhum: sem tela.

Convenções do repo que valem para todas as etapas: só biblioteca padrão, Python >= 3.10; hook
falha aberto (exceção vira linha em `~/.tokeneconomizer/errors.log`); nomes de arquivos de estado
não mudam; texto do plugin sem alegação de efeito. Testes novos seguem
`tests/test_history_import.py` (fixtures `claude_home`, `env`, `repo` em `:29-54`; `history_log`
em `:77-118` fabrica logs; `ctx_for` `:121`; `start_hook` `:138`; `ledger_ids` `:145`;
`TE_FOLD_SYNC=1` roda o import inline em vez de processo separado). `tests/conftest.py:53-70`
troca toda chamada de modelo por um stub que devolve `("", None)`.

---

## Etapa 1 — import ligado por padrão e a configuração `history_propose_sessions`

Tela: não.

Arquivos: `tokeneconomizer/plugin/config.py`, `tests/test_history_import.py`.

O que prova a etapa:
- Sem nenhuma configuração, `history_import` é verdadeiro (troca a asserção de
  `test_history_import.py:204-220`, que hoje espera falso; a parte que diz "um start com o padrão
  não grava `history.json`" passa a esperar que grave).
- `history_propose_sessions` vale 12 sem configuração; `TE_HISTORY_PROPOSE_SESSIONS=0` dá 0;
  `config set history_propose_sessions=5` dá 5; valor negativo ou não numérico é recusado como os
  outros inteiros da config.
- `TE_HISTORY_IMPORT=off` continua desligando.
- A suíte inteira continua verde (todo teste de `SessionStart` do Claude Code agora roda o import
  inline sobre um `CLAUDE_CONFIG_DIR` vazio; se algum teste quebrar por achar `history.json`
  inesperado, ajuste o teste para esperar o arquivo, não o código).

Como fazer:
- `config.py:219`: `history_import: bool = True`. Ao lado de `history_days` (`:221`), adicionar
  `history_propose_sessions: int = 12`.
- Mapa de ambiente em `config.py:364-365`: acrescentar
  `"TE_HISTORY_PROPOSE_SESSIONS": "history_propose_sessions"`.
- Seguir como os outros inteiros (`history_max_sessions`) são validados e convertidos no mesmo
  arquivo; procurar `history_max_sessions` no arquivo e repetir o tratamento.
- Docstring `config.py:71-78`: `history_import` passa a "on by default"; acrescentar
  `history_propose_sessions -- of an import's sessions, the newest this many may run the fact
  proposer (micro-skills with a paid profile only); 0 calls no model while importing`.
- `cli.py:641-645` (saída de `te config`, bloco "history"): incluir
  `"history_propose_sessions": d.pop("history_propose_sessions")`.

## Etapa 2 — o modelo de fatos roda nas sessões mais recentes do histórico

Tela: não.

Arquivos: `tokeneconomizer/plugin/fold.py`, `tokeneconomizer/plugin/history.py`,
`tests/test_history_import.py`.

O que prova a etapa:
- Com `TE_MICROSKILLS=on` (perfil pago) e 20 sessões no histórico, `history_propose_sessions=12`:
  o runner do proposer é pedido (`sidecall.runner_for(..., role="proposer")`) só para as 12 mais
  recentes, nunca para as 8 mais antigas. Use um runner falso que registra cada chamada e devolve
  `("", 0)`; com ritmo 3, ele é chamado 4 vezes (sessões 1, 4, 7 e 10 da cauda).
- `history_propose_sessions=0`: nenhum runner pedido, como hoje. Reescreve
  `test_an_import_calls_no_model_and_keeps_a_waiting_deny_list` (`:608-624`) para fixar
  `TE_HISTORY_PROPOSE_SESSIONS=0` e manter a asserção de zero chamadas; e um teste novo com o
  padrão espera chamadas só na cauda.
- Com `microskills=envfacts` (perfil sem modelo) nenhum runner é pedido mesmo na cauda.
- Com o sidecall pausado (`sidecall.paused` verdadeiro) a cauda usa `sidecall.local_runner`,
  como a sessão ao vivo faz.
- Sessão importada continua sem segurar deny list e com registro `imported` no ledger.
- O registro do ledger de uma sessão da cauda que chamou o modelo tem
  `overhead.optimizer_tokens` maior que zero quando o runner falso devolve tokens (`("", 500)`).
- Num import retomado (estado `partial`), a cauda é contada sobre o plano salvo inteiro, não
  sobre o que falta.

Como fazer:
- `fold._fold_microskills(ctx, adapter, run_dir, session_id, injected=None, imported=False)`
  (`fold.py:75`): novo parâmetro `propose=False`. A condição em `:85` vira
  `if icfg.paid and (not imported or propose):`. Atualizar o comentário de `:81-84`.
- `fold.fold_locked(ctx, session_id, transcript_path=None, imported=False, span=None)`
  (`fold.py:502`): novo parâmetro `propose=False`, repassado na chamada de `_fold_microskills`
  (perto de `:547`). Docstring: "no model is called" vira "no model is called, except the fact
  proposer when `propose` (the newest `history_propose_sessions` of an import)".
- `history._import_one(ctx, doc, item, skipped, log)` (`history.py:394`): receber o índice do
  item no plano (`_run_locked` em `:370-379` já tem `i` e `items`). Calcular
  `tail = max(0, int(ctx.config.history_propose_sessions or 0))` e
  `propose = tail > 0 and i >= len(items) - tail`. Passar `propose=propose` para
  `fold.fold_locked` em `:426`.
- Docstring do módulo `history.py:39-46`: "no model call" vira a regra nova.

## Etapa 3 — o gasto do histórico aparece em `te status`

Tela: não (saída de terminal; sem pasta de tela).

Arquivos: `tokeneconomizer/plugin/accounting.py`, `tokeneconomizer/plugin/cli.py`,
`tests/test_history_import.py` (ou o teste de `te status` que já existir; procurar
`imported_sessions` em `tests/`).

O que prova a etapa:
- `accounting.summarize` com registros importados que têm `overhead.optimizer_tokens` devolve
  `imported_overhead_tokens` igual à soma deles, e `overhead_tokens` continua sem eles (os números
  das sessões do plugin não mudam).
- Sem registros importados, `imported_overhead_tokens` é 0.
- `te status` imprime, quando `imported_overhead_tokens > 0`, uma linha
  `  history      ... ; <N> tokens spent reading it` junto da linha de histórico existente
  (`cli.py:272-274`); com 0, a linha fica como é hoje.

Como fazer:
- `accounting.summarize` (`accounting.py:36-60`): antes de filtrar os importados (`:43`),
  calcular `imp_oh = sum(overhead_total(r.get("overhead")) for r in imported)` e devolver
  `"imported_overhead_tokens": imp_oh`. Atualizar a docstring.
- `cli.py:272-274`: se `ledger["imported_overhead_tokens"]`, acrescentar
  `f"; {accounting.fmt_tokens(...)} effective tokens spent reading it"` ao texto de `hist`
  (ou imprimir a linha `history` com só esse dado se `hist` for vazio). Texto factual, sem
  alegação de efeito.

## Etapa 4 — achar todos os repos do histórico numa varredura só

Tela: não.

Arquivos: criar `tokeneconomizer/plugin/history_all.py`; mudar `tokeneconomizer/plugin/history.py`;
criar `tests/test_history_all.py`.

O que prova a etapa:
- Três pastas no histórico (repo A com 5 sessões, repo B com 2, repo C com 3), cada uma num
  diretório que existe: `discover` devolve 3 grupos, cada um com os logs certos.
- Dois checkouts do mesmo repo git (mesmo `origin`) caem num grupo só.
- Log cujo `cwd` gravado não existe mais, numa pasta do Claude Code cujo nome bate exatamente
  com a de um repo vivo do histórico: entra no grupo desse repo. Pasta `widget-shell` não pega
  log de `widget` (casamento exato).
- Log de pasta que não existe e não bate com nenhum repo vivo: fica fora, contado em
  `skipped["gone"]`.
- Logs de subagente não aparecem (já filtrados por `host.history`).
- A ordem: o repo do `first_cwd` primeiro; depois por número de logs, decrescente; empate pela
  chave.
- `log_head` é chamado uma vez por log no `discover` inteiro (contar com monkeypatch).
- `history.plan(ctx, logs=grupo["logs"])` dá o mesmo resultado que `history.plan(ctx)` para o
  mesmo repo, sem chamar `host.history` (monkeypatch que falha se chamado).

Como fazer:
- `history.plan(ctx, now=None, logs=None)` (`history.py:173`): se `logs` vier, iterar sobre ela
  (itens `{"path", "folder", "session_id", "mtime", "size", "head"}`) e usar `item["head"]` em
  vez de `ctx.host.log_head(...)`; a checagem `is_this_repository` continua (barata, com cache).
  Sem `logs`, comportamento atual.
- `history_all.discover(host, env, dirs, first_cwd=None)`:
  - `for log in host.history(env, dirs)`: `head = host.log_head(log["path"])`; `cwd =
    head.get("cwd")`.
  - Se `cwd` existe no disco (`os.path.isdir`): chave por `history._identity(cwd, env, cache)`
    (`history.py:146-153`); guarda o primeiro `cwd` existente do grupo e o `label` de
    `repo_identity(cwd, env)` (`paths.py:246`).
  - Senão, guarda o log numa lista de órfãos com `log["folder"]`.
  - Depois do laço, para cada órfão, procurar o grupo vivo cuja pasta derivada
    (`trace.claude_project_folder(repo_root)`, ver `history.py:156-164` e `hosts.py:252-255`
    `folder_matches`) é exatamente `log["folder"]`; achou, entra no grupo; não achou,
    `skipped["gone"] += 1`.
  - Devolve `{"repos": [{"key", "label", "cwd", "logs"}...] na ordem pedida, "skipped": {...}}`.
- `host` é o objeto de `hosts.py` do Claude Code (`keeps_history`); recusar com lista vazia se
  `not getattr(host, "keeps_history", False)`.
- Sem importar `tokeneconomizer.plugin` de dentro dos mecanismos: `history_all.py` mora em
  `plugin/`, então pode importar `history`, `paths`, `hosts`, `trace`.

## Etapa 5 — o import de todos os repos, com retomada, e o comando `te history import --all`

Tela: não.

Arquivos: `tokeneconomizer/plugin/history_all.py`, `tokeneconomizer/plugin/history.py`,
`tokeneconomizer/plugin/cli.py`, `tests/test_history_all.py`.

O que prova a etapa:
- `run_all` com 3 repos no histórico importa os 3, na ordem da fila; depois `history.json` de
  cada um está `done` e `history-all.json` está `done` com os 3 em `done`.
- Repo que já tem sessão aprendida ao vivo: é pulado (fica `skipped` no `history.json` dele, como
  o import por repo já faz) e o run segue para o próximo.
- Repo com `history.json` já `done`, `skipped` ou `stopped`: não é tocado.
- Exceção dentro de um repo (monkeypatch em `history.run` para um deles): vira linha em
  `errors.log`, o repo conta em `skipped["error"]`, e os outros são importados.
- Orçamento global estourado no meio (orçamento pequeno + relógio falso, como os testes de
  `BUDGET_S` existentes): `history-all.json` fica `partial` com `next` apontando para o repo
  seguinte; um segundo `run_all` retoma dali sem reimportar os anteriores.
- Um repo que ficou `partial` por orçamento próprio volta para a fila no próximo run.
- `history-all.json` com `running` e heartbeat velho (mais que `history.HEARTBEAT_STALE_S`)
  é tomado por um run novo; com heartbeat fresco, um segundo run desiste sem fazer nada.
- `history_import=off`: `run_all` com `auto` não faz nada e grava `stopped`.
- `te history import --all --dry-run` imprime uma linha por repo (`label`, sessões a ler,
  pasta) e a linha `left out: gone N`, e não grava nenhum arquivo em `~/.tokeneconomizer`.
- `te history import --all` imprime o total importado por repo e sai 0.
- O log do processo fica em `<state_root>/history-all.log`.

Como fazer:
- Estado em `os.path.join(state_root(env), "history-all.json")` (`paths.state_root`). Ler e
  gravar sob uma trava de arquivo; seguir o padrão de `Store.locked` e escrita atômica de
  `tokeneconomizer/plugin/store.py` (ex.: `Store(state_root)` com `.read`, `.write`,
  `.locked(name, timeout)`), formato:
  `{"state": "running"|"partial"|"done"|"stopped", "queue": [keys], "cwds": {key: cwd},
  "next": int, "done": [keys], "skipped": {why: n}, "started", "heartbeat", "finished", "pid"}`.
- `run_all(host, env, first_cwd=None, auto=False, budget_s=history.BUDGET_S * 4, log=None)`:
  1. Tomar o estado: se `running` com heartbeat fresco, sair (`{"state": "busy"}`). Se
     `auto` e `history_import` desligado na config global, gravar `stopped` e sair.
  2. Sem fila salva (ou estado final de run anterior com novos repos): `discover` e salvar
     `queue`/`cwds`.
  3. Para cada chave a partir de `next`: `ctx = LiveContext.build(host, cwds[key], env)`
     (`context.py:42`); se `history.read_state(ctx)` está em `done`/`skipped`/`stopped`, pular.
     Marcar o `history.json` do repo como o `maybe_start` faz (`history.py:456-475`: sem doc,
     recusa se já há sessão aprendida, senão `running` + heartbeat) usando `history._update`;
     então `history.run(ctx, auto=True, log=log)` passando os logs do grupo para o `plan`
     (acrescentar `logs=None` em `history.run` e `history._run_locked`, repassado a `plan`).
     Em exceção: `history._record_error(root)`, `skipped["error"] += 1`, segue.
     Resultado `partial`: manter a chave para o próximo run (não entra em `done`).
     Atualizar `next` e `heartbeat` depois de cada repo.
  4. Fim da fila: `done` (ou `partial` se algum repo ficou `partial` ou o orçamento acabou).
- CLI (`cli.py:1034-1040`): `p.add_argument("--all", dest="all_repos", action="store_true")`.
  Em `cmd_history` (`cli.py:727`): com `args.all_repos`, desviar para
  `history_all.cli(args)`, que faz `--dry-run` (só `discover` + `history.plan(ctx_sem_settle,
  logs=...)` com `LiveContext.build(..., settle=False)` para não gravar nada) ou `run_all`.
  Atualizar a ajuda em `cli.py:26-31`.
- Spawn do processo global: função `spawn(host, env, first_cwd)` em `history_all.py` que copia
  `LiveContext.spawn` (`context.py:73-96`) com argv `["history", "import", "--all", "--auto"]`
  e o log em `<state_root>/history-all.log`, `cwd=first_cwd`.

## Etapa 6 — disparo na primeira sessão depois de instalar

Tela: não.

Arquivos: `tokeneconomizer/plugin/history_all.py`, `tokeneconomizer/plugin/controller.py`,
`tokeneconomizer/plugin/history.py`, `tests/test_history_all.py`.

O que prova a etapa:
- Primeiro `SessionStart` do Claude Code numa máquina sem `history-all.json`: dispara o run
  global (com `TE_FOLD_SYNC=1`, inline; senão, `spawn` com argv
  `history import --all --auto`), começando pelo repo da sessão.
- Segundo `SessionStart` com o run `done`: não dispara nada.
- `SessionStart` com `history-all.json` em `partial`, ou `running` com heartbeat velho:
  dispara de novo.
- Enquanto o global está `running` com heartbeat fresco e o repo da sessão está na fila dele, o
  `history.maybe_start` daquele repo não abre um segundo processo.
- Repo novo, fora da fila do global (não tinha histórico): `history.maybe_start` funciona como
  hoje.
- `history_import=off`: nenhum dos dois dispara.
- Codex, Copilot e os outros hosts: nada muda (só hosts com `keeps_history`; o Droid também tem
  `keeps_history` e fica de fora do global: checar `host.name == "claude"`).
- Exceção dentro de `history_all.maybe_start` não muda a saída do hook (vira linha no
  `errors.log`).
- `history.notice` (a linha "TokenEconomizer read N earlier session(s)...") continua aparecendo
  uma vez por repo depois que o import dele termina.

Como fazer:
- `history_all.maybe_start(ctx)`: se `ctx.host.name != "claude"` ou
  `not ctx.config.history_import`, sai. Lê `history-all.json` sob trava curta
  (`history.HISTORY_LOCK_S`); decide disparar se não existe, se `partial`, ou se `running` com
  heartbeat velho; ao decidir, grava `running` + heartbeat agora (para outra sessão simultânea
  não disparar também). Dispara `run_all(..., first_cwd=ctx.repo.root, auto=True)` inline com
  `TE_FOLD_SYNC=1`, senão `spawn`.
- `history_all.covers(ctx)`: verdadeiro se o global está `running`/`partial` com heartbeat
  fresco e `ctx.repo.key` está na `queue` a partir de `next`.
- `controller.py:192-195`: antes de `history.maybe_start(self.ctx)`, chamar
  `history_all.maybe_start(self.ctx)` no mesmo `try`/`except` (ou num próprio); e só chamar
  `history.maybe_start` se `not history_all.covers(self.ctx)`.

## Etapa 7 — documentação, instalação nesta máquina e teste no histórico real

Tela: não.

Arquivos: `README.md` (`:414-420`), `docs/PLUGIN.md` (`:16`, `:47-69`, `:766-788`),
`plugin/README.md` (seções Settings e Earlier sessions, perto de `:874-893`),
`tokeneconomizer/plugin/controller.py:5`, `tokeneconomizer/plugin/history.py:1-8`.

O que prova a etapa:
- Suíte `python3 -m pytest tests` verde.
- Nenhum texto do repo ainda diz que `history_import` é desligado por padrão
  (`grep -rn "off by default" README.md docs plugin tokeneconomizer | grep -i history` vazio).
- Na máquina do usuário, com o plugin instalado do fork: `te history import --all --dry-run`
  lista os repos do `~/.claude/projects` e as pastas que somem.
- Com `config set microskills=on --global`, abrir uma sessão do Claude Code: o processo global
  sobe; ao fim, `te status` em 2 ou 3 repos com mais sessões mostra sessões lidas do histórico e
  o gasto do histórico, e `te microskills` mostra fatos aprendidos (candidatos ou ativos).

Como fazer:
- Docs: descrever o padrão ligado, a leitura de todas as pastas na primeira sessão,
  `te history import --all [--dry-run]`, `history_propose_sessions` (padrão 12, 0 = sem modelo)
  e que o gasto aparece em `te status`. Manter o tom factual do README, sem prometer economia.
- Instalação: `claude plugin marketplace add "$HOME/TokenEconomizer"` e
  `claude plugin install tokeneconomizer@tokeneconomizer`; depois
  `~/.tokeneconomizer/bin/te config set microskills=on --global` (ou o `te` do plugin).
- Push da branch para o fork ao fim.
