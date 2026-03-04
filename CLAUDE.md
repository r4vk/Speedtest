# Speedtest - Claude Code Configuration

## Projekt
Monitoring połączenia internetowego z web UI — historia, wykresy, eksport CSV, alerty email.
Stack: Docker (ghcr.io/r4vk/speedtest), SQLite (własna baza w volume /data), port 8000.
Przewodnik AI: [AGENTS.md](AGENTS.md)

## Infrastruktura
Projekt standalone — własny Docker, własna SQLite.
Brak zależności od shared infra r4vk.org (nie używa r4vk-postgres, r4vk-kafka, r4vk-redis).
Docker Compose: `./docker-compose.yml`

## Zasoby AI (niedostępne z poziomu tego folderu)
Skills i pluginy: `../submodules/` → indeks: `../AI_RESOURCES_INDEX.md`
Procedury: `../VibeCodingProcedures/`
Aby użyć skilla — powiedz użytkownikowi żeby przeszedł `cd ..` lub wskaż pełną ścieżkę `../submodules/[skill]/`.
