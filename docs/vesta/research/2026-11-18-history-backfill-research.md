# Pesquisa — aprender com o histórico do Claude Code desde a instalação

Spec: `docs/vesta/specs/2026-11-18-history-backfill-design.md`

## Achados

### A1 — Micro-skills vem desligado por padrão; sem ele nenhum fato é aprendido, nem do histórico
- Fonte: `tokeneconomizer/plugin/config.py:182` (`microskills: str = "off"`), `tokeneconomizer/plugin/fold.py:77-78`
- Contradiz a spec: parcial (a spec assume que fatos saem do histórico; só saem com micro-skills ligado)
- Pergunta que levanta: ligar micro-skills por padrão no fork, ou deixar como configuração que o usuário liga uma vez?

### A2 — O Claude Code apaga sessões com mais de 30 dias; o histórico desta máquina começa em 19/10/2026
- Fonte: https://code.claude.com/docs/en/claude-directory#cleaned-up-automatically ; `find ~/.claude/projects` (mais antiga 2026-10-19)
- Contradiz a spec: não (só limita o que há para ler)
- Pergunta que levanta: aumentar `cleanupPeriodDays` desta máquina para o histórico futuro não sumir antes de ser útil em outra máquina/reinstalação? (fora do código do plugin)

### A3 — Não existe gancho de instalação de plugin no Claude Code
- Fonte: https://code.claude.com/docs/en/hooks (evento `Setup` só com `--init`/`--maintenance`)
- Contradiz a spec: não (confirma disparo no primeiro `SessionStart`)
- Pergunta que levanta: nenhuma

### A4 — `te status` esconde o gasto das sessões importadas
- Fonte: `tokeneconomizer/plugin/accounting.py:41-43` (descarta registros `imported` antes de somar overhead)
- Contradiz a spec: sim (a spec promete o custo do histórico visível)
- Pergunta que levanta: precisa de uma soma nova, separada, para o usuário ver quanto o histórico gastou

### A5 — Já existe a varredura de todos os logs; ela está presa a um repo e relê tudo a cada chamada
- Fonte: `tokeneconomizer/plugin/history.py:173-214` (`plan`), `:146-153` (`_identity`), `hosts.py:348-370`, `hosts.py:233`
- Contradiz a spec: parcial (a spec descreve `discover` do zero; chamar `plan` por repo leria as 1.074 cabeças ~60 vezes)
- Pergunta que levanta: `discover` faz uma varredura só e entrega a lista pronta para o `plan` de cada repo

### A6 — Log cuja pasta sumiu ainda pode pertencer a um repo vivo, pelo nome da pasta do Claude Code
- Fonte: `tokeneconomizer/plugin/history.py:156-164`
- Contradiz a spec: parcial (a spec joga esses logs em "gone")
- Pergunta que levanta: rotear pelo nome da pasta, como o import por repo já faz, para não perder sessões de checkout movido

### A7 — Montar o contexto de cada repo tem efeito colateral (adoção e migração de estado)
- Fonte: `tokeneconomizer/plugin/context.py:42-58`, `tokeneconomizer/plugin/repos.py:612-644, 769-780`
- Contradiz a spec: não (é o mesmo que a primeira sessão no repo faria)
- Pergunta que levanta: nenhuma; aceitar e registrar erro por repo

### A8 — O log do processo em segundo plano vai para o store do repo que disparou
- Fonte: `tokeneconomizer/plugin/context.py:73-96`
- Contradiz a spec: parcial (o run global precisa de log no state root)
- Pergunta que levanta: nenhuma; log em `<state_root>/history-all.log`

### A9 — A chamada do modelo já é segura em segundo plano e não suja o histórico
- Fonte: `tokeneconomizer/plugin/sidecall.py:293-302` (`--no-session-persistence`, `TE_SIDECALL=1`, timeout 180 s, stdin pelo `input=`), `:128-146` (pausa se deslogado); risco de keychain só fora do terminal: https://github.com/anthropics/claude-code/issues/77213
- Contradiz a spec: não
- Pergunta que levanta: nenhuma (o processo nasce do hook, herda o ambiente logado)

### A10 — O primeiro tick com modelo dispara na primeira sessão da cauda
- Fonte: `tokeneconomizer/microskills/distiller.py:652-653, 826`, `stage.py:74-76`
- Contradiz a spec: não (cauda de 12, ritmo 3: chamadas nas sessões 1, 4, 7 e 10 da cauda; a janela de 8 já vem cheia das importadas anteriores)
- Pergunta que levanta: nenhuma

### A11 — Mudar o padrão de `history_import` quebra um teste e mexe em todo teste de `SessionStart`
- Fonte: `tests/test_history_import.py:204-220`, `:608-624` (assegura zero chamadas de modelo em importada), `tests/conftest.py:42-50`
- Contradiz a spec: não (prevê testes; registra quais mudam)
- Pergunta que levanta: nenhuma

### A12 — 100 chamadas saem da mesma cota de 5 h/semanal do plano
- Fonte: https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan
- Contradiz a spec: não
- Pergunta que levanta: coberto pela escolha "12 por repo"; nenhuma

Suite atual: 1244 passaram, 30 pulados, 23 s.

## Fila do grill

1. A1 — ligar micro-skills por padrão no fork? (sem isso o resto não gera fato)
2. A4 — mostrar o gasto do histórico em `te status`
3. A5 — uma varredura só, entregue ao `plan` de cada repo
4. A6 — log de pasta sumida rota pelo nome da pasta
5. A8 — log do run global no state root
