# NASOS Pairlist Injector Docker Stack

Diese Repository-Version stellt den `pairlist_injector_nasos_v5_V10.py` als fertigen Docker- und Portainer-Stack bereit.
Der Dienst scannt Maerkte auf Binance und KuCoin, bewertet Handelspaare anhand technischer Kriterien und stellt die gefundenen Paare als JSON-Endpunkte ueber HTTP bereit.

Der grosse Vorteil: Du musst den Python-Code nicht mehr anfassen, um wichtige Parameter zu aendern.
Port, Intervall, Quote-Waehrungen, Mindestvolumen und weitere Schluesselwerte koennen ueber `.env` oder Portainer-Umgebungsvariablen gesteuert werden.

## Was das Projekt macht

Der Pairlist Injector ist ein externer Marktscanner.
Er beobachtet auf Binance und KuCoin die verfuegbaren Handelspaare, laedt Kurs- und Volumendaten, bewertet die Maerkte nach fest eingebauten NASOS-/Reversal-Regeln und erzeugt daraus eine aktuelle Pairlist.

Diese Pairlist wird nicht in Dateien "ausgedruckt", sondern ueber HTTP bereitgestellt.
Andere Systeme koennen die Endpunkte regelmaessig abrufen und bekommen immer den aktuellen Stand.

Kurz gesagt:

- Der Dienst sammelt Marktdaten.
- Der Dienst filtert schlechte oder riskante Paare heraus.
- Der Dienst bevorzugt Paare mit guenstiger Struktur, Momentum und Reversal-Signalen.
- Der Dienst merkt sich seinen Zustand zwischen Neustarts.
- Der Dienst liefert die Ergebnisse als API.

## Wofuer das gut ist

Das Projekt ist sinnvoll, wenn du:

- dynamische Pairlists statt statischer Coin-Listen verwenden willst
- Binance und KuCoin parallel beobachten moechtest
- USDT, USDC oder beide Quote-Waehrungen gleichzeitig nutzen willst
- eine zentrale Quelle fuer aktuelle Handelspaare brauchst
- die Auswahl von Paaren vom restlichen System trennen willst
- einen Dienst suchst, den du sauber in Docker oder Portainer betreiben kannst

Praktischer Nutzen:

- weniger manuelle Pflege
- schnelle Umstellung per `.env`
- klar definierte HTTP-Endpunkte
- reproduzierbarer Betrieb im Container
- saubere Wiederanlauf-Faehigkeit durch gespeicherten State

## Was intern passiert

Beim Start laeuft der Dienst in groben Schritten so:

1. Er erstellt bei Bedarf automatisch eine `.env` mit Standardwerten.
2. Er laedt alle Umgebungsvariablen mit `python-dotenv`.
3. Er baut die aktive Konfiguration daraus zusammen.
4. Er liest auf Binance und KuCoin die handelbaren Symbole ein.
5. Er laedt Ticker, Volumen und Kline-Daten.
6. Er nutzt HTTP und bei Binance zusaetzlich WebSockets fuer laufende Updates.
7. Er berechnet technische Kennzahlen wie EWO, RSI, EMA, ATR, Bollinger, Volumenqualitaet und Reversal-Confidence.
8. Er filtert Paare ueber Trend-, Volumen-, Crash-, BTC- und Late-Entry-Logik.
9. Er erstellt daraus eine "sticky" Pairlist, damit gute Kandidaten nicht bei jedem Zyklus sofort herausfallen.
10. Er speichert den Zustand in `data/` und stellt die Ergebnisse ueber HTTP bereit.

## Warum die einzelnen Mechanismen sinnvoll sind

Der Dienst ist nicht einfach nur ein "Top Volume"-Sorter.
Er versucht, brauchbare Kandidaten von unguenstigen Situationen zu trennen.

Wichtige Mechanismen:

- Reversal-Confidence:
  Bewertet, ob ein Coin wirklich dreht oder nur faellt.
- BTC-Filter:
  Verhindert unguenstige Setups, wenn der Gesamtmarkt schwaechelt.
- Volumenfilter:
  Entfernt Paare ohne ausreichend Liquiditaet.
- Pump-/Fade-Schutz:
  Vermeidet Kandidaten, die nur kurzfristig hochgespuelt wurden.
- Falling-Knife-Filter:
  Blockiert Paare, die zwar "guenstig aussehen", aber noch klar fallen.
- Sticky Pairlist:
  Gute Paare bleiben fuer eine definierte Zeit erhalten und rotieren nicht zu aggressiv.
- Persistenter State:
  Nach Neustarts gehen Blacklists, Probation und Sticky-Zustaende nicht verloren.

## Unterstuetzte Boersen und Quote-Waehrungen

Unterstuetzt werden:

- Binance
- KuCoin

Quote-Waehrungen:

- nur `USDT`
- nur `USDC`
- mehrere gleichzeitig, zum Beispiel `USDT,USDC`

Wichtig:

- `PAIRLIST_BASE_CURRENCIES` hat Vorrang.
- Wenn `PAIRLIST_BASE_CURRENCIES` nicht gesetzt ist, wird `PAIRLIST_BASE_CURRENCY` verwendet.
- Dadurch kannst du entweder klassisch mit genau einer Quote-Waehrung arbeiten oder modern mit mehreren parallel.

## API-Endpunkte

### Allgemein

- `GET /health`
- `GET /details`
- `GET /banned`

### Binance

- `GET /pairs`
- `GET /pairs-binance`
- `GET /pairs-binance-usdt`
- `GET /pairs-binance-usdc`
- `GET /pairs-binance/{quote_currency}`

### KuCoin

- `GET /pairs-kucoin`
- `GET /pairs-kucoin-usdt`
- `GET /pairs-kucoin-usdc`
- `GET /pairs-kucoin/{quote_currency}`

## Bedeutung der wichtigsten Endpunkte

### `/health`

Zeigt, ob der Dienst laeuft.
Enthaelt unter anderem:

- Status
- Anzahl aktiver Paare
- Cycle-Zaehler
- letzte Datenquelle
- WebSocket-Status

### `/pairs` und `/pairs-binance`

Liefert die aktuelle Binance-Pairlist.

### `/pairs-kucoin`

Liefert die aktuelle KuCoin-Pairlist.

### Waehrungsspezifische Endpunkte

Mit diesen Links bekommst du nur Paare einer bestimmten Quote-Waehrung:

- `/pairs-binance-usdt`
- `/pairs-binance-usdc`
- `/pairs-kucoin-usdt`
- `/pairs-kucoin-usdc`

Zusaetzlich gibt es generische Varianten:

- `/pairs-binance/USDT`
- `/pairs-binance/USDC`
- `/pairs-kucoin/USDT`
- `/pairs-kucoin/USDC`

Wenn eine Quote-Waehrung aktuell nicht konfiguriert ist, bleibt die Antwort leer und `configured` ist `false`.

### `/details`

Liefert tiefere Informationen zu analysierten Paaren, Scores und internen Statistiken.
Dieser Endpunkt ist hilfreich fuer Debugging, Monitoring und Feinabstimmung.

### `/banned`

Zeigt derzeit gesperrte Paare inklusive Grund und Restlaufzeit.

## Beispiele fuer fertige Links

Wenn dein Server unter `http://192.168.1.50:9999` laeuft:

- Binance alle: `http://192.168.1.50:9999/pairs-binance`
- Binance USDT: `http://192.168.1.50:9999/pairs-binance-usdt`
- Binance USDC: `http://192.168.1.50:9999/pairs-binance-usdc`
- KuCoin alle: `http://192.168.1.50:9999/pairs-kucoin`
- KuCoin USDT: `http://192.168.1.50:9999/pairs-kucoin-usdt`
- KuCoin USDC: `http://192.168.1.50:9999/pairs-kucoin-usdc`
- Health: `http://192.168.1.50:9999/health`
- Details: `http://192.168.1.50:9999/details`

## Projektstruktur

- `pairlist_injector_nasos_v5_V10.py`
  Der eigentliche Dienst.
- `Dockerfile`
  Baut das lauffaehige Container-Image.
- `docker-compose.yml`
  Stack-Datei fuer Docker Compose und Portainer.
- `.env.example`
  Vorlage fuer die wichtigsten Umgebungsvariablen.
- `data/`
  Persistenter Zustand wie Sticky- und Blacklist-State.

## Wichtige Umgebungsvariablen

Die folgenden Variablen sind bewusst nach aussen gezogen worden:

- `PAIRLIST_HTTP_PORT`
  Port des HTTP-Dienstes.
- `PAIRLIST_UPDATE_INTERVAL`
  Zeit in Sekunden zwischen den Scan-Zyklen.
- `PAIRLIST_MAX_PAIRS`
  Maximale Anzahl auszuliefernder Paare.
- `PAIRLIST_BASE_CURRENCY`
  Einzelne Quote-Waehrung, zum Beispiel `USDT`.
- `PAIRLIST_BASE_CURRENCIES`
  Mehrere Quote-Waehrungen, zum Beispiel `USDT,USDC`.
- `PAIRLIST_MIN_VOLUME`
  Mindestvolumen in Quote-Waehrung.
- `PAIRLIST_MIN_PRICE`
  Mindestpreis eines Coins.
- `PAIRLIST_MIN_SCORE`
  Minimale Score-Schwelle fuer Kandidaten.
- `PAIRLIST_REVERSAL_ENTRY`
  Mindestschwelle fuer bestaetigtes Reversal.
- `PAIRLIST_MAX_DROP_1H`
  Falling-Knife-Grenze fuer 1h.
- `PAIRLIST_BTC_RSI_MIN`
  Mindest-RSI fuer den BTC-Marktfilter.
- `PAIRLIST_TOP_N_VOLUME`
  Extern gesetzter Volumen-Schwellwert fuer die Konfiguration.
- `PAIRLIST_MAX_VOLATILITY`
  Extern gesetzter Volatilitaetswert fuer die Konfiguration.
- `PAIRLIST_WS_ENABLED`
  Aktiviert oder deaktiviert WebSocket-Nutzung.
- `PAIRLIST_LOG_LEVEL`
  Log-Level, zum Beispiel `INFO` oder `DEBUG`.
- `PAIRLIST_STATE_FILE`
  Pfad zur gespeicherten State-Datei.
- `PAIRLIST_KUCOIN_API_BASE_URL`
  Basis-URL fuer KuCoin.

## Beispiele fuer `.env`

### Nur USDT

```env
PAIRLIST_BASE_CURRENCIES=USDT
PAIRLIST_MAX_PAIRS=20
PAIRLIST_MIN_VOLUME=3000000
PAIRLIST_UPDATE_INTERVAL=30
```

### Nur USDC

```env
PAIRLIST_BASE_CURRENCIES=USDC
PAIRLIST_MAX_PAIRS=20
PAIRLIST_MIN_VOLUME=3000000
PAIRLIST_UPDATE_INTERVAL=30
```

### USDT und USDC gleichzeitig

```env
PAIRLIST_BASE_CURRENCIES=USDT,USDC
PAIRLIST_MAX_PAIRS=20
PAIRLIST_MIN_VOLUME=3000000
PAIRLIST_UPDATE_INTERVAL=30
```

## Lokaler Start mit Docker Compose

1. `.env.example` nach `.env` kopieren.
2. Werte bei Bedarf anpassen.
3. Stack starten:

```bash
docker compose up --build -d
```

4. Logs ansehen:

```bash
docker compose logs -f
```

5. Testen:

```bash
curl http://localhost:9999/health
curl http://localhost:9999/pairs-binance
curl http://localhost:9999/pairs-kucoin
curl http://localhost:9999/pairs-binance-usdt
curl http://localhost:9999/pairs-kucoin-usdc
```

6. Stoppen:

```bash
docker compose down
```

## Portainer-Anleitung Schritt fuer Schritt

1. In Portainer `Stacks` oeffnen.
2. `Add stack` waehlen.
3. Einen Stack-Namen vergeben, zum Beispiel `pairlist-injector`.
4. Als Quelle `Repository` waehlen.
5. Repository-URL eintragen:

```text
https://github.com/ksus55s7s/pairlist-portainer-stack.git
```

6. Branch auf `main` setzen.
7. Als Compose-Datei `docker-compose.yml` verwenden.
8. Unter Umgebungsvariablen die gewuenschten Werte setzen.
9. Stack deployen.

Danach ist der Dienst ueber den konfigurierten Port erreichbar.

## Empfohlene Portainer-Konfiguration

Fuer einen stabilen Start sind diese Einstellungen sinnvoll:

- `PAIRLIST_BASE_CURRENCIES=USDT`
  fuer klassischen Betrieb
- `PAIRLIST_BASE_CURRENCIES=USDT,USDC`
  wenn beide Quote-Waehrungen gewuenscht sind
- `PAIRLIST_UPDATE_INTERVAL=30`
  guter Startwert
- `PAIRLIST_MIN_VOLUME=3000000`
  vernuenftiger Filter gegen sehr kleine Paare
- `PAIRLIST_WS_ENABLED=true`
  fuer laufende Binance-Updates

## Persistenz und Datenablage

Der Stack mountet `./data` nach `/app/data`.
Dort liegen die State-Dateien fuer:

- sticky Paare
- Blacklist
- Probation
- Volumenhistorie

Das ist wichtig, weil der Dienst dadurch nach einem Neustart nicht wieder "bei Null" beginnt.

## Was im Log zu sehen ist

Beim Start loggt der Dienst die geladene Konfiguration, zum Beispiel:

- Base currencies
- Max pairs
- Min volume
- Min price
- Update interval

Danach folgen unter anderem:

- Anzahl gueltiger Symbole pro Boerse
- Backfill-Status
- Cycle-Logs
- aktive Paare
- blockierte oder gebannte Paare

## Typische Fragen

### Warum liefert ein Endpunkt manchmal keine Paare?

Das ist nicht automatisch ein Fehler.
Moegliche Gruende:

- aktuelle Marktphase passt nicht zu den Regeln
- Volumen zu niedrig
- Reversal noch nicht bestaetigt
- BTC-Filter blockiert
- Quote-Waehrung nicht aktiv

### Warum sind auf Binance und KuCoin unterschiedlich viele Paare sichtbar?

Weil beide Boersen andere Maerkte, andere Liquiditaet und andere Datenstrukturen haben.
Die Filterlogik ist gleichartig, aber das Eingangsuniversum ist unterschiedlich.

### Was passiert bei Neustart?

Der Dienst liest seinen gespeicherten State wieder ein.
Dadurch bleiben Sticky-Paare, Sperren und Probation-Daten erhalten.

### Was ist der Unterschied zwischen `/pairs-kucoin` und `/pairs-kucoin-usdt`?

- `/pairs-kucoin` gibt alle aktuell aktiven KuCoin-Paare aus
- `/pairs-kucoin-usdt` filtert nur die USDT-Paare

## Troubleshooting

### Container startet nicht

Pruefen:

- ist Docker aktiv
- ist Port `9999` frei
- ist die `.env` korrekt
- sind Internetzugriffe auf Binance und KuCoin erlaubt

### `health` antwortet, aber es kommen keine Paare

Dann laeuft der Dienst meist korrekt, aber der aktuelle Markt liefert keine passenden Kandidaten.
In diesem Fall helfen:

- `/details` pruefen
- Logs ansehen
- Konfiguration kontrollieren

### Binance meldet `HTTP 418`

Das ist in der Regel kein Python-Syntaxfehler, sondern ein Binance-IP-Block.
Binance verwendet `418`, wenn eine IP nach Rate-Limit-Problemen automatisch gebannt wurde.

In dieser Stack-Version wird deshalb:

- fuer oeffentliche Marktdaten zuerst `data-api.binance.vision` bevorzugt
- bei `429` und `418` eine Abkuehlzeit aktiviert
- nicht mehr aggressiv ueber alle Binance-Hostnamen weitergehammert

Wenn der Fehler trotzdem bleibt, liegt das meist an der oeffentlichen Server-IP oder an anderer Software auf demselben Host, die Binance ebenfalls stark belastet.

### Keine USDC-Paare sichtbar

Pruefen:

- ist `PAIRLIST_BASE_CURRENCIES=USDT,USDC` oder `USDC` gesetzt
- rufst du den richtigen Endpunkt auf, zum Beispiel `/pairs-binance-usdc`

## Zusammenfassung

Dieses Projekt ist ein containerisierter Pairlist-Dienst fuer Binance und KuCoin.
Er scannt den Markt, filtert schlechte Bedingungen heraus, bewertet moegliche Kandidaten und stellt die Ergebnisse ueber klare HTTP-Endpunkte bereit.

Der grosse Mehrwert liegt in:

- externer Konfiguration ueber `.env`
- Betrieb in Docker und Portainer
- Unterstuetzung fuer USDT, USDC oder beide parallel
- stabiler API-Ausgabe
- persistenter State-Verwaltung
- fester URL-Struktur fuer Integrationen
