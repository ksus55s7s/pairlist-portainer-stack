# NASOS Pairlist Injector Docker Stack

Dieses Repository startet den `pairlist_injector_nasos_v5_V10.py` als Docker-Container und ist so vorbereitet, dass du es direkt in Portainer als Stack aus einem GitHub-Repository deployen kannst.

## Endpunkte

- `GET /health`
- `GET /pairs`
- `GET /pairs-binance`
- `GET /pairs-kucoin`
- `GET /details`
- `GET /banned`

Standard-Port ist `9999`.

## Lokal mit Docker Compose testen

```bash
cp .env.example .env
docker compose up --build -d
docker compose logs -f
```

Pruefen:

```bash
curl http://localhost:9999/health
curl http://localhost:9999/pairs
curl http://localhost:9999/pairs-kucoin
```

Stoppen:

```bash
docker compose down
```

## Portainer Stack

1. Repository in GitHub hinterlegen.
2. In Portainer `Stacks` -> `Add stack`.
3. `Repository` auswaehlen und dieses Repo verbinden.
4. `docker-compose.yml` als Stack-Datei verwenden.
5. Optional die Werte aus `.env.example` als Umgebungsvariablen in Portainer setzen.

## Persistenz

Die State-Dateien werden unter `./data` gespeichert und im Container nach `/app/data` gemountet.

## Wichtige Umgebungsvariablen

- `PAIRLIST_HTTP_PORT`
- `PAIRLIST_UPDATE_INTERVAL`
- `PAIRLIST_MAX_PAIRS`
- `PAIRLIST_BASE_CURRENCY`
- `PAIRLIST_WS_ENABLED`
- `PAIRLIST_LOG_LEVEL`
- `PAIRLIST_STATE_FILE`
- `PAIRLIST_KUCOIN_API_BASE_URL`
