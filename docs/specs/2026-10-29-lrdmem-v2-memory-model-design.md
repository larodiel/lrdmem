# lrdmem v2 — modelo de memória associativa

Documento de design. Data: 2026-10-29. Autor da ideia: Victor Larodiel.
Evolui o lrdmem v0.1 (`docs/specs/2026-10-28-lrdmem-design.md`, código em `main`).

## 1. Intenção e porquê

O v0.1 guarda regras (memórias) ligadas aos erros que as criaram (contextos), injeta por gatilho e roda
obrigações aprovadas. Victor quer que o lrdmem se comporte como um cérebro que aprende sozinho, "como uma criança
aprendendo coisas novas":

1. **Memória persistente** — registrar informações e experiências além de uma execução.
2. **Memória associativa** — unidades conectadas por relações explícitas e tipadas.
3. **Consolidação incremental** — transformar experiências em conhecimento reutilizável considerando evidências,
   duplicatas, contradições e resultados.
4. **Recuperação contextual** — entregar ao agente as memórias mais relevantes, com conexões úteis e a confiabilidade
   de cada uma.

Agentes compartilham conhecimento dentro de um projeto; projetos ficam isolados. **Nada de treinar modelos ou mexer
em pesos**: todo aprendizado vem da memória externa e de como ela é organizada e atualizada.

O lrdmem continua **local**: arquivos Markdown num repositório git do usuário, CLI em Python stdlib, sem servidor.
Um serviço ("Cortex", com API, worker e banco) é um produto futuro, depois de validar este modelo.

### Modelo mental (referência, não biologia literal)

| Analogia | No lrdmem |
|---|---|
| Neurônio | **Memory** — fato, episódio, procedimento, hipótese, intenção, observação sobre o agente |
| Sinapse | **MemoryEdge** — relação tipada entre duas memórias, com evidências, confiança, validade, histórico |
| Rede neural | o grafo de memórias e relações |
| Experiência | **Experience** — evento observado por um agente (execução, decisão, erro, descoberta, resultado) |
| Consolidação | revisão de experiências e memórias que atualiza relações e promove conhecimento com evidência suficiente |
| Recuperação | localizar memórias e conexões relevantes para a tarefa atual |
| Esquecimento seletivo | reduzir prioridade de temporário/obsoleto sem apagar evidência nem histórico |

**Três conceitos que nunca se confundem:** uma *experiência* é uma observação histórica (imutável); uma *memória* é
uma representação reutilizável (evolui); uma *relação* é como duas memórias se conectam (evolui).

## 2. Metas

- G1. Entidades `Experience`, `Memory`, `MemoryEdge`, `MemoryEvidence`, `MemoryRevision` (§4), cada uma com
  identidade estável.
- G2. Seis tipos de memória; o modelo permite evoluir mecanismos específicos por tipo sem migração.
- G3. Relações tipadas com estado (provisória vs validada), evidências e histórico; podem ser revisadas, invalidadas
  e substituídas sem perder o passado.
- G4. Ciclo de vida completo (§5): registro → candidatos → associação → duplicata/conflito → relações →
  consolidação → feedback.
- G5. Consolidação **automática**, sem aprovação humana; toda mudança relevante gera uma revisão reversível.
- G6. Recuperação (§6) combinando lexical (BM25), entidades, contexto, estado, confiança, validade e grafo (≤2 saltos),
  com limites configuráveis e resposta estruturada.
- G7. Esquecimento seletivo por prioridade.
- G8. **Todo commit automático segue Conventional Commits** (§7).
- G9. Store v0.1 continua válido sem migração manual (§8).

## 3. Não-metas

- Treino de modelo, fine-tuning, alteração de pesos.
- Busca vetorial / embeddings (decisão de Victor: ele busca por palavras-chave e nomes de arquivo; vetor fica para o
  Cortex ou para uma dependência opcional futura). O desenho de recuperação deve permitir somar um sinal vetorial
  depois sem mudar a resposta.
- Servidor, API HTTP, banco de dados, worker em processo de fundo.
- Chamar um LLM por conta própria: o "LLM" do ciclo é o próprio agente (Claude, Codex, OpenCode) seguindo a skill.
- Mecanismos especializados completos para os seis tipos no MVP (ex.: lembretes de prospectiva, autoavaliação de
  metacognição) — só o tipo e o ciclo comum.

## 3.1 Anti-metas

- Fundir memórias só porque o texto é parecido.
- Tratar hipótese causal como fato, ou similaridade como causalidade.
- Promover memória por contagem de acessos ou por repetição de vários agentes sem evidência independente.
- Apagar memória por um feedback negativo isolado.
- Sobrescrever informação silenciosamente: toda alteração importante é rastreável e reversível.
- Apagar experiência ou histórico.
- Vazar memória entre projetos/ambientes.
- Commit automático fora de Conventional Commits.

## 4. Modelo de dados (arquivos no store)

Mesmo layout de escopo do v0.1 (`_global/`, `<env>/_global/`, `<env>/<repo>/`). Dentro de cada pasta de escopo:

```
<scope>/
  MEMORY.md                       índice das memórias ativas (como hoje)
  <memory-id>.md                  Memory
  experiences/<YYYY-MM-DD>-<id>.md  Experience (imutável)
  edges/<edge-id>.md              MemoryEdge
  revisions/<YYYY-MM-DD>-<id>.md  MemoryRevision
```

IDs estáveis: memória = slug (como hoje); experiência, aresta e revisão = `<YYYY-MM-DD>-<curto>` com sufixo em
colisão. Referências entre escopos usam caminho relativo ao store (`work/app/phpcs`), regra de escopo do v0.1
(um nó só referencia o próprio ambiente ou `_global`).

### Experience (imutável)

```yaml
id: 2026-10-29-migration-failed
date: 2026-10-29T14:03:00Z
kind: error | decision | discovery | outcome | execution | correction | review | feedback
agent: codex
device: notebook
repo: shop-frontend
files: [src/Migrations/Drop.php]
source: chat | pr | run | check
ref: https://github.com/o/r/pull/12#discussion_r1      # opcional
request_id: <uuid>                                     # idempotência
```
Corpo: o que foi observado, em texto livre. Nunca editada depois de gravada (lint acusa alteração via git).
Os *contextos* do v0.1 são lidos como experiências `kind: correction`.

### Memory

```yaml
name: drop-column-after-deps
description: Atualize dependências antes de remover colunas | update deps before dropping columns
metadata:
  type: feedback|project|user|reference     # v0.1, mantido
  memory_type: episodic | semantic | procedural | hypothesis | prospective | metacognitive
  state: candidate | active | contested | superseded | obsolete
  valid_from: 2026-10-29                    # opcional
  valid_until: 2027-01-01                   # opcional
  author: codex
  created: 2026-10-29
  updated: 2026-10-29
triggers: {paths: [...], commands: [...], repos: [...]}   # v0.1
check: {run: ..., on: stop|post-edit}                      # v0.1, só procedural
evidence:
  - {ref: work/app/experiences/2026-10-29-migration-failed, kind: supports, weight: 1.0}
  - {ref: feedback:2026-11-02-x, kind: refutes, weight: 0.5}
```
Corpo: conteúdo + **Why:**/**How to apply:** (v0.1). `metadata.kind` do v0.1 mapeia: `obligation` → `procedural`
com `check`; `pattern` → `procedural`; `fact` → `semantic`. Memória sem `memory_type`/`state` é lida como
`semantic`/`active`.

`confidence` e `priority` **não são gravadas**: são calculadas na leitura a partir das evidências, do estado e das
datas (§5.6, §6.3). O arquivo só é reescrito quando estado, conteúdo, validade ou evidências mudam — evita reescrita
em massa e conflitos de sync entre dispositivos.

O parser de frontmatter do v0.1 só aceita um nível de aninhamento e listas inline de escalares; `evidence` é uma
lista de objetos. **Decisão do builder**: estender o parser (listas inline de objetos `{k: v}`) ou guardar evidências
em seção do corpo; precisa round-trip estável e lint.

### MemoryEdge

```yaml
id: 2026-10-29-e7
source: work/app/drop-column-after-deps
target: work/app/migration-failed-oct
type: related_to | derived_from | occurred_in | supports | contradicts | caused_by | resolved_by | generalizes | supersedes
confidence: 0.4
weight: 0.5            # relevância para recuperação, opcional
state: proposed | validated | invalidated | superseded
valid_from / valid_until: opcionais
created / updated
evidence: [...]        # mesmo formato da memória
superseded_by: <edge-id>   # opcional
```
`[[links]]` no corpo de memórias v0.1 são lidos como arestas `related_to` validadas implícitas (não geram arquivo).
Similaridade nunca vira `caused_by`/`supports` sozinha.

### MemoryEvidence

Não é arquivo próprio: é a lista `evidence` em memória/aresta. Cada item referencia uma experiência, PR, execução de
check, revisão ou feedback, com `kind: supports | refutes | observed_in` e `weight`. **Independência**: evidências da
mesma sessão de agente contam como uma (chave = `agent + device + data` da experiência, decisão do builder se houver
id de sessão melhor).

### MemoryRevision

```yaml
id: 2026-10-29-r3
target: work/app/drop-column-after-deps        # memória ou aresta
action: create | merge | state_change | confidence_change | content_change | invalidate | supersede | revert
before: {...}   # campos alterados, valores antigos
after:  {...}
reason: "2 evidências independentes de 2 agentes; sem contradição"
by: consolidate | <agent>
```
`lrdmem revert <revision-id>` aplica o inverso e grava uma revisão `revert`. Git é o histórico de fundo; a revisão é o
histórico semântico.

## 5. Ciclo de vida

### 5.1 Registro de experiência
`lrdmem experience add --kind ... --agent ... [--files] [--source] [--ref] [--request-id] -- "<texto>"` (ou JSON no
stdin). Grava o arquivo **antes** de qualquer processamento. Mesmo `request_id` → no-op que devolve o id existente.
Valida segredos, escopo e symlinks como o `learn` do v0.1.

### 5.2 Extração de candidatos
O agente (via skill) propõe candidatos derivados da experiência: episódio, observação/fato, hipótese, procedimento,
intenção, observação metacognitiva. Exemplo do texto de Victor: "migração falhou porque coluna removida antes da
dependência" → episódio (falhou), observação (coluna removida antes), **hipótese** (ordem causou), procedimento
candidato (verificar deps antes de remover colunas). Comando: `lrdmem memory propose` (JSON no stdin com os
candidatos e a experiência de origem). **O código valida** antes de gravar: tipos e estados permitidos, hipótese nunca
entra como `semantic`, campos obrigatórios, escopo, segredos. Todo candidato que o agente extrai sozinho nasce `state: candidate`
com evidência `observed_in` para a experiência e aresta `derived_from` para o episódio.

**Correção explícita do humano nasce ativa** (decisão de Victor): memória vinda de uma experiência `correction`
(chat, `learn`) ou `review` (`learn-pr`, autor confiável) entra `state: active` e é injetada na hora; a experiência
conta como evidência forte. Hipóteses nunca entram ativas, mesmo vindas de correção.

### 5.3 Associação
Para cada candidato o CLI devolve memórias potencialmente relacionadas: BM25 (conteúdo, descrição, gatilhos),
entidades conhecidas (arquivos, símbolos, comandos, ids), vizinhos no grafo, mesmo repo e proximidade temporal — cada
uma com os sinais que a trouxeram. A associação gera **arestas `proposed`**, nunca validadas.

### 5.4 Duplicata e conflito
O agente classifica cada candidato vs memórias associadas: `new | equivalent | complementary | contradicts |
insufficient`. Aplicação:
- `equivalent` → não cria memória; adiciona a experiência como evidência da existente (+ revisão).
- `complementary` → cria a memória e aresta `related_to`/`generalizes` proposta.
- `contradicts` → cria (ou mantém) as duas, aresta `contradicts`, estado `contested` na que tiver evidência mais fraca.
- `insufficient` → mantém `candidate`.
- Na dúvida → memórias separadas + aresta proposta.
Similaridade textual sozinha nunca funde.

### 5.5 Relações
Arestas têm proveniência e estado explícito. Promoção `proposed → validated` só na consolidação (§5.6). Invalidar ou
substituir uma aresta gera revisão e mantém o arquivo.

### 5.6 Consolidação (`lrdmem consolidate`)
Comando idempotente e tolerante a falhas (pode ser interrompido e rodado de novo; cada passo é atômico por arquivo).
Roda automaticamente no fim da sessão (hook Stop, sem bloquear) e sob demanda. Duas partes:

**Determinística (código):**
- recalcula `confidence` de memórias e arestas a partir das evidências: soma ponderada de `supports` independentes
  menos `refutes`, saturando em 1 (fórmula exata é do builder; deve ser monotônica, testável e documentada);
- promoção automática: `candidate → active` quando ≥2 evidências `supports` independentes (agentes ou dispositivos
  ou datas distintas) e nenhuma `contradicts` validada; `hypothesis` só vira `semantic`/`procedural` (nova memória
  `generalizes`, a hipótese fica `superseded`) com evidência de **resultado** (`kind: outcome|check`) além de
  observações; `proposed → validated` para arestas com ≥2 evidências independentes;
- `contested` quando surge `refutes`/`contradicts` com peso comparável; volta a `active` se as evidências
  favoráveis dominarem de novo;
- validade: `valid_until` vencido → `obsolete`;
- procedimentos repetidos: ≥3 experiências independentes resolvidas do mesmo jeito sem procedimento ligado → fila;
- duplicatas exatas (mesmo conteúdo normalizado no mesmo escopo) → fusão automática com revisão `merge`;
- atualiza `priority` (§6.3).

**Julgamento (agente):** o que o código não decide entra numa fila (`<scope>/.queue/` ou arquivo único por escopo,
decisão do builder): possíveis duplicatas parafraseadas, contradições a interpretar, hipóteses a avaliar, arestas
propostas sem evidência suficiente, procedimentos a generalizar. Na sessão seguinte o session-start injeta "N decisões
pendentes" e a skill instrui o agente a resolvê-las com `lrdmem consolidate --resolve <item> <decisão>`. Tudo
automático, sem aprovação humana (decisão de Victor). Cada decisão aplicada gera revisão.

Nunca promove por número de acessos ou repetições do mesmo agente. Considera qualidade/independência das evidências,
resultados observados e o contexto de validade.

### 5.6.1 Execução da consolidação
- **No Stop, inline e com orçamento de tempo** (chamada minha): só a parte determinística, até ~20 s dentro dos 330 s
  do hook (depois dos checks), retomável — o que não couber fica para a próxima execução. Roda **antes** do
  autopush, para entrar no mesmo commit. No OpenCode roda no fim da execução da sessão com o orçamento que o plugin
  permite. Sem processo em background. Um lock por store (arquivo em `$TMPDIR/lrdmem-<uid>/`) impede duas execuções
  simultâneas na mesma máquina.
- **Fila de julgamento compartilhada pelo git** (chamada minha): um arquivo por item em `<scope>/queue/<item-id>.md`;
  a decisão é idempotente por `item-id` — a primeira aplicada vence, resolver de novo em outro dispositivo é no-op
  (o item vira `resolved` com a revisão que o resolveu).

### 5.6.2 Captura automática de checks
Os hooks gravam sozinhos uma experiência `kind: check` por obrigação que rodou (passou/falhou, memória, arquivos),
deduplicada por sessão + memória + resultado (decisão de Victor). Ela vira evidência `supports` (passou depois de
falha corrigida) ou `refutes`/`observed_in` conforme o caso, e é o principal sinal de **resultado** para promover
hipóteses e procedimentos. Nada mais é capturado automaticamente; o resto vem do agente via skill.

### 5.7 Feedback
`lrdmem feedback <memory-id> useful|incorrect|irrelevant [--note] [--agent]` grava uma experiência `kind: feedback` e
uma evidência (`supports` para useful, `refutes` para incorrect, nada de verdade para irrelevant — só reduz
prioridade naquele contexto). Um feedback negativo isolado nunca muda estado; a consolidação considera contexto e
exceções. A skill instrui o agente a dar feedback quando uma memória recuperada ajudou ou atrapalhou.

## 6. Recuperação

### 6.1 Comando
`lrdmem recall [QUERY] [--file F] [--command C] [--types ...] [--states ...] [--limit N] [--hops 2] [--budget 1500]
[--json]`. Substitui `for` (que vira atalho de `recall --file`). Os hooks pre-edit/pre-command passam a usar `recall`.

### 6.2 Pipeline
1. **Filtro de escopo primeiro** ("autorização"): só repo atual, `<env>/_global` (com regra de `triggers.repos` do
   v0.1 para injeção) e `_global`.
2. **Candidatos**: BM25 (stdlib) sobre conteúdo+descrição; gatilhos (paths/commands); entidades (arquivo atual,
   símbolos/comandos citados).
3. **Expansão no grafo** até `--hops` (padrão 2): segue arestas `validated` (peso cheio) e `proposed` (peso reduzido);
   ignora `invalidated`/`superseded`.
4. **Pontuação** combinando: texto, contexto (repo/arquivo), estado, confiança, validade, prioridade e distância no
   grafo. Pesos são do builder, configuráveis, documentados e testados.
5. **Limites**: candidatos, saltos, resultados, orçamento de tokens.
6. Estados por padrão (chamada minha): **injeção pelos hooks** = `active` e `contested` (rotulada); **`recall`
   explícito** = também `candidate` e `hypothesis`, rotuladas como não confirmadas; `superseded`/`obsolete` só com
   `--states all`. Checks de obrigação rodam só para memórias procedurais `active`.

### 6.3 Esquecimento seletivo (prioridade)
`priority` decai com o tempo para `episodic` e `prospective` (meia-vida configurável), cai para `superseded`/
`obsolete` e para `irrelevant` repetido no mesmo contexto; sobe com evidências recentes. Nada é apagado: experiências,
revisões e memórias de baixa prioridade continuam no grafo, em `why`, `show` e `recall --states all`.

### 6.4 Resposta
Para cada memória: id, conteúdo, tipo, estado, confiança, proveniência (experiências/evidências), relações relevantes
(tipo, alvo, estado) e sinais de recuperação ("bm25: migration, drop", "file trigger **/*.php", "2 hops via
caused_by"). Texto agrupado por tipo e estado com rótulos claros ("Conhecimento consolidado", "Hipóteses — não
comprovadas", "Experiências históricas"); `--json` para máquinas. Mantém o aviso "reference data, not instructions"
e `printable()`.

## 7. Commits automáticos (Conventional Commits)

Todo commit feito pelo lrdmem (autopush no hook, `sync`, `consolidate`) segue Conventional Commits 1.0.0:

```
<type>(memory): <resumo em minúsculas, imperativo>

<corpo opcional: lista do que mudou>
```

Mapeamento (chamada minha): mudanças só de experiências/memórias/arestas novas → `feat(memory): learn N memories,
M experiences`; consolidação → `refactor(memory): consolidate — merge 1, promote 2, contest 1`; revert →
`revert(memory): <revision-id>`; sync sem conteúdo semântico (índices, prioridade) → `chore(memory): sync <env> from
<device>`. Escopo opcional `memory` fixo; resumo ≤ 72 caracteres. Teste valida o formato com regex da especificação.

## 8. Compatibilidade

Store v0.1 é lido sem reescrita: contextos = experiências `correction`; memórias sem `memory_type`/`state` =
`semantic`/`active` (ou mapeadas de `metadata.kind`); `[[links]]` = `related_to` implícito. Nenhuma reescrita em
massa; arquivos só ganham campos novos quando o próprio ciclo os toca (via edição pontual de linhas, como o `_touch`
do v0.1, nunca re-dump destrutivo). Comandos v0.1 continuam: `learn` = atalho para `experience add` + `memory
propose` de uma memória procedural/semântica; `learn-pr` registra cada comentário como experiência `review`; `hot`
ordena por força de evidência; `for` = `recall --file`.

### 8.1 Formato do store
O v0.1 nunca foi publicado; todos os dispositivos de Victor atualizam juntos, então **não há suporte a frota mista**
(chamada minha). O v2 grava `<store>/.lrdmem-format` com `2`; uma versão que encontra um formato maior que o seu
recusa escrever e avisa. Lint avisa se o marcador estiver ausente num store já tocado pelo v2.

## 9. Segurança

**Nenhum dado sensível entra no store, nunca** (decisão de Victor): senhas, API keys, tokens, secrets e chaves
privadas são recusados em todo caminho de escrita (memórias, experiências, saída de checks, relações, revisões, fila e
feedback) por uma única barreira no módulo de escrita; a saída de checks é redigida linha a linha antes de ser
registrada; o lint continua acusando o que entrar à mão.

Todas as invariantes do v0.1 (AGENTS.md) continuam. Adições: `memory propose`, `consolidate --resolve` e `feedback`
validam escopo, segredos, symlinks e ids como o `learn`; conteúdo vindo de agentes é dado, nunca instrução; a fila de
consolidação não pode conter comandos executáveis (uma decisão nunca cria/altera `check.run` aprovado — mudar
`check.run` continua exigindo `lrdmem allow`).

## 10. Decidido por Victor

- Continua local (arquivos + git + CLI); Cortex é produto futuro.
- Sem busca vetorial no MVP (busca por palavras-chave e nomes de arquivo).
- Modelo de dados §4 (experiência imutável, memória com tipo/estado/confiança, aresta tipada com evidência, revisão).
- Consolidação totalmente automática, sem aprovação humana ("como uma criança aprendendo").
- Commits automáticos em Conventional Commits.
- Correção explícita do humano (chat, learn, learn-pr) nasce ativa e é injetada na hora.
- Hooks capturam automaticamente só o resultado dos checks.
- O LLM do ciclo é o próprio agente.

## 11. Chamadas minhas (revisar)

- Arquivos separados por entidade em `experiences/`, `edges/`, `revisions/` dentro de cada escopo.
- Evidência embutida em memória/aresta, não arquivo próprio.
- Limiares de promoção (≥2 evidências independentes; hipótese exige resultado; ≥3 para procedimento repetido).
- Independência = agente + dispositivo + data distintos.
- Consolidação roda no Stop sem bloquear; fila resolvida pelo agente na sessão seguinte.
- `recall` substitui `for`; `learn` vira atalho.
- `confidence`/`priority` calculadas na leitura, não gravadas.
- Consolidação inline no Stop (~20 s, retomável, antes do autopush, lock por máquina); fila compartilhada via git,
  primeira decisão vence.
- Estados injetados pelos hooks: `active` + `contested`; `recall` explícito inclui candidatas/hipóteses rotuladas.
- Sem frota mista; marcador `.lrdmem-format`.
- Mapeamento de tipos de commit (§7).

## 12. Fica para o builder

Formato exato de evidências no frontmatter (ou corpo), fórmula de confiança e de pontuação, pesos e meia-vida
padrão, layout da fila, implementação de BM25, nomes de flags secundárias, formato do texto de `recall`.
