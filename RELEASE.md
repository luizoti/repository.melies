# Release — atualizar addons e o repositório

Todos os comandos rodam de `/home/luiz/projetos/python/KODI/addons/repository.melies` salvo indicação contrária.

## 1. Atualizar um addon (subprojeto)

Ex.: `skin.xperience1080` (submodule em `omega/` e `piers/`).

1. Entre no submodule e faça as mudanças:
   ```bash
   cd omega/skin.xperience1080
   git checkout -b fix/xyz   # se detached HEAD: git checkout -b main && git switch main ... use a branch do remoto
   git add .
   git commit -m "fix: descrição"
   git push origin <branch>
   cd ../..
   ```
2. Bump de versão no `addon.xml` do submodule (quando for release nova):
   ```xml
   <addon id="skin.xperience1080" version="10.1.0-osd" ...>
   ```
   Commit + push junto com o passo 1.

## 2. Atualizar o ponteiro do submodule no repo pai

```bash
git add omega/skin.xperience1080 piers/skin.xperience1080   # árvores onde o addon entra
git commit -m "chore: bump omega + piers skin.xperience1080"
git push origin master
```

## 3. Gerar a release

```bash
# validação (não escreve nada)
python3 _repo_generator.py --tree omega --force --clean --dry-run

# aplica: zips + addons.xml(.md5) + patch repository.melies + index.html + cleanup
python3 _repo_generator.py --tree omega --force --clean
```

Para todas as árvores: `python3 _repo_generator.py --all --force --clean`.

### Flags
| Flag | Efeito |
|---|---|
| `--tree NAME` | só uma árvore (`matrix`/`omega`/`piers`) |
| `--all` | todas as árvores existentes |
| `--force` | re-zipa mesmo se zip atualizado |
| `--clean` | remove zips órfãos; `--keep N` define quantos zips antigos manter (default 5) |
| `--dry-run` | valida e mostra ações, não escreve |
| `--no-sync` | não clona sources faltantes (aborta se árvore não existe) |
| `--bump-repo` | ++ `repository_version` em `addons.yaml` e patcheia `repository.melies` |

Bump do próprio `repository.melies` (quando houve mudança no repo add-on):
```bash
python3 _repo_generator.py --all --force --clean --bump-repo
```

## 4. Publicar

```bash
git add omega/zips piers/zips matrix/zips omega/repository.melies piers/repository.melies index.html addons.yaml
git status          # confira o que entrou
git commit -m "release: build omega|piers|matrix"
git push origin master
```

`repo_base` aponta para `raw.githubusercontent.com/luizoti/repository.melies/master` → tudo precisa estar em `master` para o Kodi baixar.

## Ordem completa para uma release de skin

```bash
# 1. submodule: código + bump version
cd omega/skin.xperience1080
git add . && git commit -m "fix: ..." && git push origin <branch>
# 2. pai: ponteiro
cd ../..
git add omega/skin.xperience1080 piers/skin.xperience1080
git commit -m "chore: bump skin.xperience1080" && git push origin master
# 3. build
python3 _repo_generator.py --tree omega --force --clean --dry-run
python3 _repo_generator.py --tree omega --force --clean
# 4. publicar
git add omega/zips piers/zips index.html addons.yaml
git commit -m "release: skin.xperience1080 <nova-versão>" && git push origin master
```