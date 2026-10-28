# lrdmem — memória compartilhada entre agentes de código

Documento de design. Data: 2026-10-28. Autor da ideia: Victor Larodiel.
Nome `lrdmem` (decidido por Victor). Projeto pessoal em https://github.com/larodiel/lrdmem.

## 1. Intenção e porquê

Victor programa em dois dispositivos (notebook e desktop) com três harnesses: Claude Code,
Codex e OpenCode. Os agentes repetem erros já corrigidos: estilo de código, padrões de
arquitetura, passos obrigatórios (ex.: rodar `phpcs`, que "diversas vezes não roda"),
especialmente em bug fixes e correções de segurança.

Diagnóstico dele: **o agente não encontra a memória e continua errando**. As correções
acontecem no chat e em revisão de PR.

O que a pesquisa diz (resumo, ver §10):
- memória entre sessões ajuda muito (DreamBench-SWE: 12% → 46–54% de acerto);
- lições destiladas de acertos **e erros** batem histórico bruto (ReasoningBank);
- agentes seguem o que está no contexto, mas a obediência cai conforme a sessão cresce
  (~5,6% por função gerada); carregar contexto genérico demais reduz acerto e custa +20%;
- grafo só ganha de busca por similaridade em perguntas estruturadas (o que substituiu X,
  conjunto completo, negação).

Conclusões que guiam o design:
1. **Obrigação não é memória.** "Sempre rode phpcs" vira checagem executada por hook, não
   conselho que o modelo pode esquecer.
2. **Memória certa na hora certa.** Injetar por gatilho (arquivo/pasta/comando que o agente
   está tocando), não só no início da sessão.
3. **O porquê é um nó.** Cada correção é um *contexto* (neurônio) ligado a uma ou mais
   memórias. Vários contextos = memória forte = candidata a virar obrigação.

É um projeto open source novo: CLI + skill + hooks, independente do repositório de dados.
Qualquer pessoa instala e aponta para o próprio repositório git de memórias.

## 2. Metas

- G1. Um repositório git de memórias compartilhado por Claude Code, Codex e OpenCode, em
  vários dispositivos.
- G2. Três tipos de conhecimento com tratamentos diferentes:
  - **obrigação** → comando executado automaticamente por hook;
  - **padrão** (estilo, arquitetura) → injetado quando um gatilho casa;
  - **fato** (sobre a codebase) → consultável pelo grafo.
- G3. Contextos como nós próprios, com proveniência (o que errou, a correção, repo, arquivo,
  agente, data, origem chat/PR), ligados a memórias.
- G4. CLI para navegar o grafo (§6).
- G5. Captura no chat (agente grava direto, sem aprovação) e a partir de PR (`learn-pr`).
- G6. Sincronização: `sync` manual sempre disponível; push automático opcional por dispositivo.
- G7. Uma skill única (formato SKILL.md) que ensina o protocolo aos três agentes.
- G8. O repositório atual de Victor (`~/agent-memory`) funciona
  **sem migração**: memórias existentes são válidas como estão (sem contextos, tipo inferido).

## 3. Não-metas

- Busca vetorial / embeddings, servidor, banco de dados, serviço em nuvem.
- UI própria além de exportar o grafo (Obsidian e um HTML estático cobrem a visualização).
- Extrair memórias automaticamente de transcripts inteiros (estilo Mem0). A captura é
  disparada por correção.
- Resolver conflitos de git sozinho.
- Suporte a Gemini CLI, Cursor etc. na v1 (o desenho de adaptadores deve permitir depois).

## 4. Anti-metas (falhou mesmo que funcione)

- Encher o contexto: a injeção por gatilho tem orçamento fixo (ver §7) — despejar todas as
  memórias é falha.
- Hook lento: qualquer hook que atrase visivelmente a sessão (alvo < 300 ms sem rede) é falha.
- Vazar escopo: memória de um ambiente aparecer em repo de outro ambiente, ou dado
  pessoal entrar em pasta de ambiente.
- Gravar segredo: credencial, token ou senha em memória ou contexto (inclusive citação do
  chat).
- Push surpresa: commit/push sem o autopush estar ligado naquele dispositivo.
- Duplicatas que divergem: duas memórias sobre o mesmo assunto.

## 5. Modelo de dados (repositório de memórias)

Markdown + frontmatter YAML, um arquivo por nó. O store é dividido em **ambientes**
(ex.: `profissional`, `pessoal`, `tests`); cada projeto pertence a um ambiente. Cada ambiente
é seu próprio repositório git (pode ter remote ou não), para ser compartilhado sozinho; a raiz
do store guarda `_global/`. Compatível com o layout atual de Victor (empresa = ambiente):

```
_global/                       fatos sobre o usuário, valem em tudo
<ambiente>/_global/               valem em todos os projetos do ambiente
<ambiente>/<repo>/                valem em um repositório
  MEMORY.md                    índice, uma linha por memória
  <slug>.md                    memória
  contexts/<YYYY-MM-DD>-<slug-curto>.md   contexto
```

`<repo>` = nome do diretório do checkout principal (igual em worktrees):
`basename "$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"`.
Repo sem pasta própria não participa (opt-in), como hoje.

### Memória

```yaml
---
name: phpcs-before-done
description: PHP repos: rode phpcs nos arquivos alterados antes de dar a tarefa como pronta
metadata:
  type: feedback            # user | feedback | project | reference (como hoje)
  kind: obligation          # obligation | pattern | fact  — novo; ausente = inferido (§5.3)
  author: claude-code
  updated: 2026-10-28
triggers:                   # novo, opcional
  paths: ["**/*.php"]
  commands: ["git commit", "gh pr create"]
check:                      # só para kind: obligation
  run: "vendor/bin/phpcs {files}"
  on: stop                  # stop | post-edit
supersedes: [old-slug]      # opcional
---
<o fato>

**Why:** ...
**How to apply:** ...
```

Links entre memórias: `[[slug]]` no corpo (como hoje). Força = número de contextos ligados,
**calculada**, nunca gravada.

### Contexto (neurônio)

```yaml
---
id: 2026-10-28-phpcs-skipped
date: 2026-10-28
agent: codex
device: notebook           # nome configurado por dispositivo
repo: shop-frontend
files: [src/Tickets/Seating/Service.php]
source: chat               # chat | pr
pr: https://github.com/org/repo/pull/123#discussion_r1   # se source: pr
memories: [phpcs-before-done]
---
**Errou:** deu a tarefa como pronta sem rodar phpcs.
**Correção:** "você esqueceu o phpcs" (Victor, no chat).
```

Contexto mora na pasta mais estreita que contém a memória que ele reforça. Se liga memórias
de escopos diferentes, fica no escopo mais estreito (decisão do builder se isso gerar caso
estranho).

### 5.3 Inferência para memórias antigas

Sem `kind`: `type: user|reference` → `fact`; `feedback|project` → `pattern`. Sem `triggers`:
a memória só aparece no índice de início de sessão e na busca (comportamento atual).
Nenhuma reescrita automática dos arquivos existentes.

## 6. CLI

Python ≥ 3.11, **somente stdlib** (YAML do frontmatter: parser mínimo para o subconjunto
usado, ou JSON-compatível — decisão do builder, sem dependência externa). Instalável com
`pipx`/`uv tool`. Grafo montado em memória a cada execução lendo os arquivos; sem cache
até medir que é lento.

Leitura:
- `lrdmem find <termo>` — memórias e contextos que casam, com vizinhos.
- `lrdmem show <slug>` — memória, contextos ligados (o porquê), memórias relacionadas.
- `lrdmem for <arquivo> [--command <cmd>]` — o que vale ao tocar nesse arquivo/comando no
  repo atual. É o comando que os hooks chamam.
- `lrdmem why <slug>` — linha do tempo dos contextos.
- `lrdmem hot [--min N]` — memórias com mais contextos (padrão N=3); marca as que são
  `pattern` como candidatas a `obligation`.
- `lrdmem stale` — memórias que citam arquivo/caminho/flag/branch inexistente.
- `lrdmem graph [--format html|json|dot]` — exporta o grafo (HTML estático autocontido).
- `lrdmem context --agent <nome>` — saída de início de sessão (índices + problemas do lint),
  equivalente ao `bin/memory context` atual.
- `lrdmem lint` — frontmatter, índices, links quebrados ou que saem do ambiente, segredos,
  caminhos mortos, contexto apontando para memória inexistente.

Escrita (usada pelos agentes e por humanos):
- `lrdmem learn --memory <slug> --mistake ... --correction ... [--files ...] [--source chat]`
  — cria o contexto; cria a memória se não existir (com `--kind`, `--description`, corpo);
  atualiza o `MEMORY.md`. Antes de criar memória nova, procura duplicata por termos e
  recusa com sugestão se achar (flag `--new` força).
- `lrdmem learn-pr <url>` — lê comentários de review via `gh`, cria um contexto por comentário
  relevante, agrupa com memórias existentes; comentários sem memória correspondente são
  listados para o agente decidir (o agente roda isso e escreve as memórias pela skill).
- `lrdmem check [--on stop|post-edit] [--files ...]` — roda os `check.run` das obrigações
  cujos gatilhos casam; código de saída ≠ 0 se algum falhar.

Sincronização:
- `lrdmem sync [<ambiente>]` — para cada ambiente (ou só o indicado): pull (rebase), commit
  do que mudou, push. Ambiente sem remote só faz commit. Em conflito: para e avisa.
- `lrdmem config autopush on|off [--env <ambiente>]` — por dispositivo e por ambiente, em
  `~/.config/lrdmem/config.json` (também guarda: raiz do store, nome do dispositivo,
  allowlist de obrigações).
- `lrdmem install --agent claude|codex|opencode [--dry-run]` — instala hooks e a skill,
  **substituindo** os hooks do `bin/memory` atual. Para Claude Code, assume o symlink
  `~/.claude/projects/<slug>/memory` → `<ambiente>/<projeto>/` que o `bin/memory` cria hoje.
  `bin/memory` é aposentado; Gemini CLI fica sem hook até existir adaptador (não-meta v1).
- `lrdmem allow [<slug>]` — lista obrigações cujo `check.run` é novo ou mudou e aprova
  (guarda hash do comando no dispositivo).

## 7. Hooks (por harness)

Comportamento alvo, igual nos três:

| Momento | Ação |
|---|---|
| início da sessão | se autopush on: `git pull` (silencioso, sem bloquear em falha de rede); `lrdmem context` |
| antes/depois de editar arquivo | `lrdmem for <arquivo>` → injeta só memórias com gatilho que casa e ainda não injetadas nesta sessão; orçamento ~1.500 tokens |
| depois de editar | `lrdmem check --on post-edit` |
| fim da tarefa (stop) | `lrdmem check --on stop`; se falhar e o harness permitir, bloqueia e devolve a saída ao agente; se autopush on, `lrdmem sync` |
| mensagem do usuário | lembrete curto: "se isto corrige algo seu, registre com `lrdmem learn`" |

- Claude Code: `SessionStart`, `PreToolUse`/`PostToolUse` (Edit/Write), `Stop`,
  `UserPromptSubmit`.
- Codex: hooks equivalentes que existirem na versão atual; onde não houver `Stop` bloqueante,
  `check` roda em post-edit. O builder verifica a API atual.
- OpenCode: plugin JS mínimo que chama o CLI nos eventos equivalentes. O builder verifica a
  API atual de plugins.

"Ainda não injetadas nesta sessão" exige estado por sessão: arquivo temporário por id de
sessão (decisão do builder).

## 8. Skill

Um `SKILL.md` único, instalado nos três harnesses. Ensina:
- quando registrar: correção no chat, review de PR, usuário reestrutura/reescreve código,
  escolha incomum confirmada (o protocolo "Learning" atual de Victor);
- registrar a **regra geral**, não o incidente; o incidente vai no contexto;
- antes de criar memória, `lrdmem find` para ligar a uma existente;
- quando consultar o CLI (`for`, `find`, `why`);
- nunca gravar segredo; o escopo mais estreito; proveniência sempre.

## 9. Segurança e privacidade

- Lint bloqueia padrões de credencial em memórias e contextos; `learn` recusa gravar.
- Citações do chat em contextos passam pelo mesmo filtro.
- Pastas de ambiente não podem linkar para fora do ambiente.
- `check.run` executa comandos de arquivos que agentes e colegas escrevem: **allowlist local**.
  Um comando novo ou alterado (hash diferente) não roda até ser aprovado com `lrdmem allow`
  naquele dispositivo; enquanto isso o hook avisa que há obrigação pendente de aprovação.
  Depois de aprovado roda sozinho. Vale para todos os ambientes.

## 10. Qualidade

- TDD; testes com `unittest` da stdlib; repositório de exemplo como fixture.
- Docblocks em funções públicas.
- Uma feature por módulo (grafo, cli, sync, hooks, lint), com testes próprios.
- Licença MIT (chamada minha).

## 10.1 Distribuição

Um único workflow de release (GitHub Actions, disparado por tag `vX.Y.Z`) publica tudo a
partir da mesma versão:

- **PyPI** — base de todos os canais: `pipx install lrdmem` / `uv tool install lrdmem`.
- **Homebrew tap** — repositório `larodiel/homebrew-tap`; o workflow atualiza a fórmula
  (url + sha256 do sdist) a cada release. `brew install larodiel/tap/lrdmem`. Python vem
  como dependência da fórmula.
- **.deb** — gerado no CI e anexado ao GitHub Release (`Depends: python3 (>= 3.11), git`).
  Instala com `sudo apt install ./lrdmem_X.Y.Z_all.deb`.

Depois da v1 (não-meta agora): repositório APT próprio (GitHub Pages/Cloudsmith) ou PPA,
para `apt install lrdmem`; homebrew-core quando o projeto tiver tração; Debian oficial fora
do escopo.

Requisito para o builder: `lrdmem --version` e um teste de fumaça (`lrdmem lint` num store
de exemplo) rodam em cada artefato no CI antes de publicar.

## 11. Decidido por Victor

- Projeto open source novo, separado do repositório de dados.
- Agente grava memórias e contextos direto, sem aprovação.
- Push automático liga/desliga por opção; sync manual sempre disponível.
- Captura vem do chat e de PR.
- Harnesses: Claude Code, Codex, OpenCode; dispositivos: notebook e desktop.
- Store dividido em ambientes (profissional, pessoal, tests); projeto pertence a um ambiente.
- lrdmem substitui o `bin/memory` e os hooks atuais.
- Obrigações só executam após aprovação local por hash (allowlist).
- Distribuição via Homebrew e apt (além de pipx/uv).
- Nome `lrdmem`; projeto pessoal em github.com/larodiel/lrdmem.

## 12. Chamadas minhas (revisar)

- Python stdlib, MIT.
- Três tipos (`obligation | pattern | fact`) e gatilhos por glob/comando.
- Limite de promoção = 3 contextos, só sugere (não promove sozinho).
- Orçamento de injeção ~1.500 tokens por evento.
- Contextos em `contexts/` dentro do escopo da memória.
- Cada ambiente = um repo git; autopush por dispositivo **e** por ambiente.

- `{files}` = arquivos que o agente editou nesta sessão (estado por sessão) que casam com
  `triggers.paths`; comando roda na raiz do repo de código. Stop bloqueia no máximo 2 vezes
  por sessão; depois só avisa (evita loop em código legado). Alvo de 300 ms vale só para
  hooks de injeção, não para `check`.
- `triggers.commands` só injeta memória (hook antes de rodar comando shell, onde o harness
  tiver); não executa `check`. `check.on` continua `stop | post-edit`.
- Escopo dentro do ambiente: campo opcional `triggers.repos: [..]`. Memória de `<ambiente>/_global/`
  sem `triggers.repos` nunca é injetada por gatilho, só aparece no índice de início de sessão
  (comportamento atual, onde o agente lê o prefixo "TEC repos:"). Sem migração, sem vazamento.
- `learn` exige `--scope repo|env|global`; o CLI resolve a pasta a partir do repo atual e
  valida: em pasta de ambiente recusa caminho de home, link para fora do ambiente e segredo. Repo
  com mesmo nome em dois ambientes → erro pedindo `--env`.

## 13. Fica para o builder

Parser de frontmatter, layout de módulos, formato do HTML do grafo, detalhes de cada API de
hook, estado por sessão, heurística de duplicata, mensagens do CLI.

## Fontes

DreamBench-SWE arXiv:2608.20664 · ReasoningBank arXiv:2509.25140 · Evaluating AGENTS.md
arXiv:2602.11988 · Instruction Adherence arXiv:2605.10039 · MOOSEDev arXiv:2608.13662 ·
MemSecBench arXiv:2607.27080 · prior art: Ki-Seki/agent-memory, Casper5253/agent-memory,
basic-memory, claude-mem.
