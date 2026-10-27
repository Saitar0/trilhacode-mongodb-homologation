# Executor isolado de homologação MongoDB 7

Este diretório é um template para um repositório GitHub separado, sem conexão com o repositório principal, Coolify ou ambientes da aplicação. Ele contém somente configuração de execução e verificação; o workflow está limitado a `workflow_dispatch`, `contents: read`, runner hospedado efêmero e pacote com SHA externo fixado.

## Estado inicial seguro

`config/bundle-lock.json` fixa o SHA de origem, o hash externo do ZIP e identifica este repositório executor. O transporte continua com `enabled: false`; assim, o workflow falha fechado antes de baixar ou executar qualquer pacote. Não habilite o transporte sem uma autorização separada e revisão da configuração.

## Preparação depois de autorização de publicação

1. Mantenha este repositório independente; não o conecte ao Coolify nem copie secrets da Trilha Code.
2. Publique no repositório executor, como asset de uma release fixa, somente o ZIP produzido por `ops/editorial-generations/build_bundle.py`. A tag é `editorial-generations-mongodb7-<12 primeiros caracteres do SHA de origem>` e o asset é `editorial-generations-mongodb7-<SHA completo>.zip`.
3. Confira o SHA-256 do ZIP fora do próprio manifesto e atualize o lock para `executor_repository`, `release_tag`, `asset_name`, `source_sha`, `sha256` e `transport.enabled: true`. Revise e aprove essa alteração de configuração antes de disponibilizá-la na branch padrão do repositório executor.
4. O workflow baixa apenas a tag e o nome de asset derivados do lock fixado, do próprio `GITHUB_REPOSITORY`. Ele compara o hash externo antes de validar o manifesto e extrair o conteúdo.
5. Disponibilize o workflow e o lock revisado na branch padrão `main` do executor e execute manualmente nessa mesma branch, marcando `confirm_disposable_run`. O pacote executado continua identificado pelo SHA de origem fixado no manifesto/lock; não há seleção livre de URL ou branch de desenvolvimento.

O job só é executado quando o dispatch usa `main` do repositório executor. Disparos em outra ref ficam sem jobs executáveis.

O template não contém o pacote da aplicação, credenciais, secrets, nem mecanismo de publicação. O `GITHUB_TOKEN` efêmero é utilizado somente pela CLI `gh` para baixar um asset do próprio repositório com `contents: read`; não é gravado no checkout.

## Pacote atualmente fixado

- Origem: `Saitar0/trilhacodeprod` no commit `f049d3ebdb5aaa5ee099dc188196500e53a55e41`.
- Release: `editorial-generations-mongodb7-f049d3ebdb5a`.
- Asset: `editorial-generations-mongodb7-f049d3ebdb5aaa5ee099dc188196500e53a55e41.zip`.
- SHA-256 externo: `16f13dccdc3b08026bb88149a46024a483f5ee8c68eeee77e0466c5834222dc6`.
- Hash de conteúdo: `30fc9ed06aa73eca17c794484bf1b08a47d9b7789e88d1fd73eaa52a0d05ee61`.
- `transport.enabled` permanece `false`; esta publicação não habilita execução.

## Orçamento do workflow

O job tem limite de 45 min. Checkout, setup Python e verificação/extração têm 1 min cada; a execução principal tem limite de 36 min; o cleanup externo tem 5 min. A execução do pacote limita `docker compose up` a 32 min e reserva até 3 min para cleanup interno. Assim, a soma máxima configurada deixa 1 min de margem do job e reserva a janela de cleanup.

## Integridade e origem

O builder lê blobs de um commit Git exato e usa allowlist estrita. ZIP, manifesto e arquivos têm ordenação, timestamps e modos normalizados. O executor compara o SHA-256 do arquivo com o valor independente no lock antes de aceitar o manifesto. Isso prova que os bytes correspondem ao valor aprovado no lock; **não prova por si só a origem confiável** e não é assinatura digital. A origem depende da revisão humana e das permissões/proteções do repositório executor.

## Transporte comparado

* **Release asset no repositório executor (recomendado):** referência por tag e nome fixos, hash verificado, histórico de produção de pacote separado do produto. O arquivo pode ser substituído na release, mas a substituição não passa no hash fixado sem também mudar o lock revisado.
* **Artefato de Actions:** conveniente e efêmero, porém depende de retenção, permissões e run ID; só use com identificador de execução fixo no lock e verificação de hash. Não é configurado neste template.
* **Branch exclusiva de snapshot:** pode conter somente o pacote em um commit, mas é mais fácil publicar histórico/código além do recorte por engano e exige checkout/ref de outra origem. Não recomendada sobre a branch de desenvolvimento original.

## Isolamento da execução

O pacote mantém `mongo:7` standalone, rede Compose interna, tmpfs para `/data/db`, nenhuma porta publicada e nenhum volume persistente. O runner cria projeto e senha aleatórios; a senha é mascarada no log antes de ser exportada ao ambiente. O script de execução recusa configuração Mongo externa, valida contexto Docker e Compose, e o cleanup verifica contexto, projeto e labels antes de remover recursos delimitados. O executor supervisiona cada teste integrado em processo isolado, aplica limites de inatividade e deadline e reserva tempo próprio para cleanup. Falhas mantêm evidências temporárias e apresentam detalhes redigidos. Não há `docker system prune`, socket montado no container, SSH, deploy, publicação de imagens ou chamadas à aplicação.

## Verificação local

Na raiz da worktree do produto:

```powershell
python ops/editorial-generations/test_bundle.py
python ops/editorial-generations/test_docker_context.py
python ops/editorial-generations/build_bundle.py --source-sha <SHA-completo> --output <caminho-fora-da-worktree>.zip
```

O builder se recusa a sobrescrever saída existente, exige `origin` do repositório autorizado e inclui exatamente a allowlist definida no verificador. Os testes de pacote usam dados sintéticos. Eles não substituem os testes integrados executados contra MongoDB 7 real.
