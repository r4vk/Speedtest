# Speedtest - Przewodnik dla AI Agentów

## Zasoby AI — lokalizacja (niedostępne z tego poziomu)

Skills i pluginy są w folderze WYŻEJ:
- `../submodules/` → pełny indeks: `../AI_RESOURCES_INDEX.md`

Aby użyć skilla: powiedz użytkownikowi żeby przeszedł `cd ..` do r4vk.org
lub wskaż pełną ścieżkę `../submodules/[skill-name]/`.

## Rekomendowane skille dla Speedtest

| Skill | Lokalizacja | Do czego |
|-------|-------------|----------|
| `backend-patterns` | `../submodules/everything-claude-code/skills/backend-patterns/` | Wzorce dla usług Docker |
| `coding-standards` | `../submodules/everything-claude-code/skills/coding-standards/` | Standardy kodu |

## Uwaga: projekt standalone

Ten projekt ma własny Docker Compose (`./docker-compose.yml`) i własną bazę danych (SQLite w volume `/data`).
Nie wymaga uruchamiania shared infra z `../scripts/`.
