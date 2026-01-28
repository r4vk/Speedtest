# DSM 7 SPK (wrapper na Docker)

Ten katalog zawiera minimalny pakiet **SPK** dla DSM 7, który:

- pobiera z GitHub Releases plik `.tar` z obrazem Dockera dla Twojej architektury,
- robi `docker load`,
- uruchamia/stopuje kontener z poziomu DSM (Package Center).

## Build SPK

```bash
cd Speedtest/spk
./build.sh 0.0.1
```

Wynik: `dist/r4vk-speedtest_0.0.1.spk`

## Install (manual)

Package Center → Manual Install → wybierz `.spk`.

Wymagane: zainstalowany **Container Manager** (DSM 7) / dostępny CLI `docker`.

## Konfiguracja

Po instalacji (DSM):

- plik konfiguracyjny: `/var/packages/r4vk-speedtest/etc/config.env`
- potem zrestartuj pakiet.

