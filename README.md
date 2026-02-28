# Network Analytics Platform / Plateforme d'analyse réseau

[![Python](https://img.shields.io/badge/python-3.9%2B-blue?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-888?logo=windows&logoColor=white)](https://docs.microsoft.com/windows)
[![dpkt](https://img.shields.io/badge/dpkt-1.9.8%2B-2ea043)](https://github.com/kbandla/dpkt)
[![PyArrow](https://img.shields.io/badge/PyArrow-15.0%2B-e67e22)](https://arrow.apache.org/docs/python/)
[![Pydantic](https://img.shields.io/badge/Pydantic-v2-e74c3c)](https://docs.pydantic.dev/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)](https://fastapi.tiangolo.com/)
[![pytest](https://img.shields.io/badge/pytest-8.0%2B-0a9edc)](https://docs.pytest.org/)
[![license](https://img.shields.io/badge/license-%20%20GNU%20GPLv3%20-green?style=plastic)](https://docs.pytest.org/)


**EN** — Real-time network traffic capture and analysis on a single edge node. Medallion architecture (Bronze / Silver / Gold), sub-second latency, zero JVM dependency.

**FR** — Capture et analyse du trafic réseau en temps réel sur un nœud edge. Architecture medallion (Bronze / Silver / Gold), latence sub-seconde, aucune dépendance JVM.

---

## Platform support / Support des plateformes

| Platform / Plateforme | Status | Notes |
|------------------------|--------|--------|
| **Windows**            | ✅ Tested / Testé | Primary development and CI target. Npcap required. |
| **Linux**              | ❌ Not tested / Non testé | AF_PACKET supported in code; not validated on this project. |
| **macOS**              | ❌ Not tested / Non testé | Not currently validated. |

> **Disclaimer (EN)** — This project is currently tested and maintained only on **Windows**. Linux and macOS are not part of the current test matrix. Use on those platforms at your own risk; contributions to add CI and validation are welcome.
>
> **Avertissement (FR)** — Ce projet est actuellement testé et maintenu uniquement sur **Windows**. Linux et macOS ne font pas partie de la matrice de tests actuelle. L’utilisation sur ces plateformes est à vos risques ; les contributions pour ajouter CI et validation sont les bienvenues.

---

## Test coverage / Couverture des tests

**EN** — Test suite: **104 tests**, total coverage **~55%** (source: `pytest --cov=src` on Windows). Execution **< 2 s**. Coverage is highest on pipeline, storage, and parsing; capture and web entrypoints are partially covered (integration and OS-dependent paths excluded).

**FR** — Suite de tests : **104 tests**, couverture totale **~55 %** (source : `pytest --cov=src` sous Windows). Exécution **< 2 s**. La couverture est la plus élevée sur le pipeline, le stockage et le parsing ; la capture et les points d’entrée web sont partiellement couverts (intégration et chemins dépendants de l’OS exclus).

```bash
# Run tests with coverage report / Lancer les tests avec rapport de couverture
python -m pytest tests/ --cov=src --cov-report=term-missing -v
```

| Module (summary)        | Coverage | Tests |
|-------------------------|----------|--------|
| `storage/` (Bronze, Silver, Gold) | ~88–100% | Stores, compaction, retention |
| `monitoring/` (metrics, logging)  | ~67–80% | Pipeline metrics, structured log |
| `capture/` (agent, collector)     | ~27–38% | Parser, schema; capture loop not in CI |
| `web/` (FastAPI app)               | ~74%    | API and routes; web_main excluded |

---

---

## Architecture Overview / Vue d’ensemble de l’architecture

**EN** — Capture layer (NIC → OS backend → ThreadPool + dpkt) feeds a multiprocessing queue; a DataSink process writes Bronze NDJSON, Silver/Gold Parquet, live snapshot and metrics; a TUI dashboard consumes the snapshot.

**FR** — La couche de capture (NIC → backend OS → ThreadPool + dpkt) alimente une file multiprocessing ; un processus DataSink écrit Bronze NDJSON, Silver/Gold Parquet, snapshot live et métriques ; un dashboard TUI consomme le snapshot.

```mermaid
flowchart TB
    subgraph CAPTURE["Capture Layer"]
        NIC["NIC Ethernet"]
        BE["OS Backend: AF_PACKET or Npcap"]
        TP["ThreadPool + dpkt parsing"]
    end

    subgraph TRANSPORT["Transport"]
        Q["mp.Queue 200k slots"]
    end

    subgraph SINK["DataSink Process"]
        direction TB
        VAL["Pydantic Schema"]
        B["Bronze NDJSON"]
        S["Silver Parquet"]
        G["Gold Parquet"]
        SNAP["LiveSnapshot _live.json"]
        MET["PipelineMetrics _metrics.json"]
    end

    subgraph DASHBOARD["Dashboard TUI"]
        TUI["Live / Ports / Stats"]
    end

    NIC --> BE --> TP --> Q --> VAL
    VAL --> B
    VAL --> S
    VAL --> G
    S --> SNAP
    G --> SNAP
    SNAP -.-> TUI
    MET -.-> TUI

    style CAPTURE fill:#1a1a2e,stroke:#0f3460,color:#e0e0e0
    style TRANSPORT fill:#16213e,stroke:#0f3460,color:#e0e0e0
    style SINK fill:#0f3460,stroke:#533483,color:#e0e0e0
    style DASHBOARD fill:#533483,stroke:#e94560,color:#e0e0e0
```

---

## Medallion Data Flow

Each packet traverses all three layers **in the same loop tick** — no intermediate file reads between layers.

```mermaid
flowchart LR
    RAW["Raw Packet"] --> BRONZE["Bronze NDJSON"]
    BRONZE --> SILVER["Silver Parquet"]
    SILVER --> GOLD["Gold Aggregates"]

    BRONZE -.- B_META["Manifest, Quarantine, Retention 72h"]
    SILVER -.- S_META["Schema, Subnet /24, Retention 7d"]
    GOLD -.- G_META["packet_count, bytes, Retention 30d"]

    style RAW fill:#2d3436,stroke:#636e72,color:#dfe6e9
    style BRONZE fill:#6c5ce7,stroke:#a29bfe,color:#fff
    style SILVER fill:#00b894,stroke:#55efc4,color:#fff
    style GOLD fill:#fdcb6e,stroke:#f39c12,color:#2d3436
```

---

## Launch / Run / Lancement

**EN** — Run from the **project root** (where `run.py`, `src/`, `requirements.txt` are). Activate the venv before any command.

**FR** — Lancer depuis la **racine du projet** (où se trouvent `run.py`, `src/`, `requirements.txt`). Activer le venv avant toute commande.

---

### Windows

| Méthode | Commande / démarche | Rôle |
|--------|----------------------|------|
| **1. Bootstrap** | `.\setup.ps1` (PowerShell en admin) | Crée `.venv`, installe les dépendances. |
| **2. Script Python (tout-en-un)** | `.\.venv\Scripts\Activate.ps1` puis `python run.py` | Capture + pipeline + **TUI** (terminal). Lance en admin si nécessaire (Npcap). *Ne lance pas l’interface web.* |
| **2b. Script Python (sans TUI)** | `python run.py --no-dashboard` | Capture + pipeline uniquement ; utile si l’UI est la web. |
| **3. Exécutable .exe** | Lancer le binaire PyInstaller (ex. `networkInterface.exe`) en admin | Même stack que le script ; un seul processus. |
| **4. Interface web** | `python -m src.network_interface.web.web_main` | **Pour l’UI navigateur** : démarre la capture si `_live.json` absent ou périmé, puis sert l’UI sur `http://127.0.0.1:8000`. |
| **5. Composants seuls** | `python -m network_interface.capture.capture_agent` (capture seule) ; `python -m network_interface.monitoring.dashboard` (TUI seul, lit `_live.json`) | Pour debug ou déploiement découplé. |

**EN** — Output files are written at **project root**: `_live.json`, `_metrics.json`, `_metrics_capture.json`, `bronze/`, `silver/`, `gold/`. Run as Administrator if using Npcap.

Fichiers produits à la **racine du projet** (répertoire qui contient `run.py` et `src/`) : `_live.json`, `_metrics.json`, `_metrics_capture.json`, dossiers `bronze/`, `silver/`, `gold/`. **`_live.json`** n’est jamais ailleurs : c’est toujours ce dossier (défini par `NI_PROJECT_ROOT` ou par le chemin du projet).

---

### Linux / macOS

| Méthode | Commande / démarche | Rôle |
|--------|----------------------|------|
| **1. Bootstrap** | `./setup.sh` | Crée `.venv`, installe les dépendances. |
| **2. Script Python (tout-en-un)** | `sudo python run.py` ou, avec venv, `sudo .venv/bin/python run.py` | Capture (droits raw) + pipeline + TUI. |
| **2b. Script Python (sans TUI)** | `sudo python run.py --no-dashboard` | Capture + pipeline uniquement. |
| **3. Interface web** | `python -m src.network_interface.web.web_main` (après activation du venv) | Idem Windows : démarre la capture si besoin, puis UI sur `http://127.0.0.1:8000`. |
| **4. Composants seuls** | `sudo python -m network_interface.capture.capture_agent` ; `python -m network_interface.monitoring.dashboard` | Capture en root/cap_net_raw ; TUI en user. |

Même emplacement des sorties : racine du projet (`_live.json`, `_metrics*.json`, `bronze/`, `silver/`, `gold/`).

---

### Emplacement des données (état du projet)

Tous les chemins sont résolus par rapport à la **racine du projet** (dossier contenant `src/` et `run.py`) :

- **`_live.json`** : snapshot live (capture + web lisent/écrivent ce fichier à la racine).
- **`_metrics_capture.json`**, **`_metrics.json`** : métriques pipeline.
- **`bronze/`**, **`silver/`, `gold/`** : données medallion.

Lancer **toujours depuis la racine** (ex. `python run.py` ou `python -m src.network_interface.web.web_main`). Si une ancienne copie de `_live.json` existait ailleurs (ex. dans `src/`), la supprimer pour éviter de lire des données périmées.

---

### Running tests / Lancer les tests

**EN** — From project root with venv activated. For coverage: `python -m pytest tests/ --cov=src --cov-report=term-missing -v`.

**FR** — Depuis la racine du projet, venv activé.

```bash
python -m pytest tests/ -v
# With coverage / Avec couverture :
python -m pytest tests/ --cov=src --cov-report=term-missing -v
```

---

## Process Architecture

```mermaid
flowchart LR
    subgraph MAIN["Main Process"]
        SIG["Signal Handler"]
        CT["Capture Thread"]
        PP["Parser Pool"]
        SL["Stats Loop"]
    end

    subgraph SINK_PROC["DataSink Process"]
        QR["Queue Reader"]
        BZ["BronzeStore"]
        SS["SilverStore"]
        GA["GoldAccumulator"]
        LS["LiveSnapshot"]
        PM["PipelineMetrics"]
        HK["Housekeeping"]
    end

    CT --> PP
    PP --> Q2["mp.Queue"]
    Q2 --> QR
    QR --> BZ
    QR --> SS
    QR --> GA
    GA --> LS
    SS --> LS
    SIG -.-> CT
    SIG -.-> QR

    style MAIN fill:#2d3436,stroke:#636e72,color:#dfe6e9
    style SINK_PROC fill:#0f3460,stroke:#533483,color:#e0e0e0
```

### GIL Bypass Strategy

| Stage | Thread/Process | Why GIL is not a bottleneck |
|---|---|---|
| **Capture** | Dedicated thread | `recvfrom()` / `pcap_next_ex()` are C calls — GIL released |
| **Parsing** | ThreadPool (N workers) | `dpkt` uses `struct.unpack()` — C extension, GIL released |
| **I/O Sink** | Dedicated OS process | Own interpreter, own GIL — zero contention |

---

## Capture Backends

```mermaid
flowchart LR
    subgraph LINUX["Linux"]
        direction TB
        L1["AF_PACKET SOCK_RAW"]
        L2["bind NIC"]
        L3["recvfrom"]
        L1 --> L2 --> L3
    end

    subgraph WINDOWS["Windows"]
        direction TB
        W1["ctypes wpcap"]
        W2["pcap_open_live"]
        W3["pcap_next_ex"]
        W1 --> W2 --> W3
    end

    L3 --> DPKT["dpkt Ethernet"]
    W3 --> DPKT

    style LINUX fill:#0984e3,stroke:#74b9ff,color:#fff
    style WINDOWS fill:#6c5ce7,stroke:#a29bfe,color:#fff
```

| Feature | Linux | Windows |
|---|---|---|
| Mechanism | Kernel-native `AF_PACKET` | Npcap `wpcap.dll` via `ctypes` |
| Device resolution | Direct NIC name | Registry GUID lookup |
| Privileges | `CAP_NET_RAW` or root | Administrator |
| Promiscuous mode | Supported | Enabled by default |

### Couche de capture (OSI)

La capture s’effectue au **layer 2 (Data Link / Ethernet)** : le noyau (Linux) ou Npcap (Windows) livre des **trames Ethernet brutes** (frame entière). Le parsing (dpkt) décode ensuite :

| Couche | Rôle |
|--------|------|
| **L2 (Ethernet)** | Réception : `AF_PACKET` / `pcap_open_live` → trames brutes. |
| **L3 (IP)** | Extraction : IP source/dest, TTL, protocole ; paquets non-IP (ARP, etc.) sont ignorés. |
| **L4 (TCP/UDP)** | Extraction : ports, flags TCP, payload pour détection de service (ex. TLS SNI → HTTPS). |

En résumé : **capture au L2**, **analyse L3 + L4**. Les paquets sans en-tête IP (`eth.data` non-IP) sont rejetés dans `parse_raw_packet` et ne figurent pas dans Bronze/Silver/Gold ni dans le dashboard.

---

## Dashboard TUI

Three modal views, all fitted to terminal height. Switch with hotkeys or CLI subcommands.

```mermaid
stateDiagram-v2
    [*] --> Live
    Live --> Ports: P key
    Live --> Stats: S key
    Ports --> Live: L key
    Ports --> Stats: S key
    Stats --> Live: L key
    Stats --> Ports: P key
    Live --> [*]: Q key
    Ports --> [*]: Q key
    Stats --> [*]: Q key
```

| View | Hotkey | Content |
|---|---|---|
| **Live** | `L` | htop-style scrolling packet table (newest at bottom) |
| **Ports** | `P` | Port/service breakdown with protocol, packets, bytes, distribution bar |
| **Stats** | `S` | Protocol split, top source/dest IPs, subnets, Gold hourly aggregates |

**Data source**: `_live.json` — an atomic JSON sidecar written every 300ms by the capture agent. Dashboard read time: **< 1ms**.

---

## Observability

### Structured Logging (`structured_log.py`)

Every log line is a JSON object, ready for Splunk HEC / ELK / Loki:

```json
{
  "ts": "2026-02-25T14:32:01.123456+00:00",
  "level": "INFO",
  "logger": "capture_agent",
  "msg": "Stats | seen=45000 queued=44800 q_size=12",
  "correlation": "a1b2c3d4e5f6",
  "agent_id": "a1b2c3d4e5f6",
  "pid": 12345,
  "thread": "MainThread"
}
```

Log files: `capture.log` (main process), `pipeline.log` (DataSink process).

### Pipeline Metrics (`pipeline_metrics.py`)

`_metrics.json` is atomically written every 2 seconds:

```mermaid
flowchart LR
    subgraph METRICS["_metrics.json"]
        direction TB
        C["Counters: captured, parsed, dropped"]
        R["Rates: packets/sec"]
        L["Latency p50 p95 p99"]
        E["Recent Errors"]
        QD["Queue Depth"]
    end

    style METRICS fill:#2d3436,stroke:#636e72,color:#dfe6e9
```

---

## Data Lifecycle

```mermaid
flowchart TB
    subgraph WRITE["Write Path real-time"]
        direction LR
        W1["Capture"] --> W2["Queue"]
        W2 --> W3["Bronze Disk"]
        W2 --> W4["Silver Disk"]
        W2 --> W5["Gold Disk"]
    end

    subgraph MAINTAIN["Housekeeping"]
        direction LR
        M1["Compaction 10min"]
        M2["Retention 1h"]
        M3["Schema Evolution"]
    end

    subgraph RETAIN["Retention"]
        direction LR
        R1["Bronze 72h"]
        R2["Silver 7d"]
        R3["Gold 30d"]
    end

    WRITE --> MAINTAIN
    M2 --> RETAIN

    style WRITE fill:#00b894,stroke:#55efc4,color:#fff
    style MAINTAIN fill:#0984e3,stroke:#74b9ff,color:#fff
    style RETAIN fill:#636e72,stroke:#b2bec3,color:#dfe6e9
```

### Schema Evolution

Old Parquet files remain readable after schema changes:

| Scenario | Handling |
|---|---|
| New column added | Filled with typed nulls |
| Column removed | Dropped silently |
| Type changed | Safe cast, or null fallback |

Powered by `_coerce_schema()` — applied at compaction and at read time via `read_parquet_with_evolution()`.

---

## Exploitation et interprétation des données

Les données capturées (Bronze NDJSON, Silver/Gold Parquet, `_live.json`) peuvent être exploitées au-delà du dashboard actuel. Pistes concrètes pour ne pas sous-utiliser la capture :

### Alertes et seuils

- **Drop rate** : alerter (email, webhook, log structuré) si `capture_health.drop_rate_pct` dépasse un seuil (ex. > 2 %).
- **Volume anormal** : comparer `total_packets` / `total_bytes` par fenêtre (5/10/30/60 min) à une baseline ou à la même fenêtre la veille ; alerte si écart > X %.
- **Nouveaux top IPs** : détecter une IP source ou destination qui apparaît dans `top_src_ips` / `top_dst_ips` sans être dans une liste connue (blanc / gris) → possible nouveau service ou anomalie.

### Baselines et profils

- **Profil “habituel”** : sur Silver/Gold (ou agrégats `_live.json`), calculer par heure/jour de la semaine : volume moyen, top 10 IPs, top 10 ports, répartition proto. Stocker en JSON ou Parquet.
- **Déviation** : en temps réel ou en batch, comparer la fenêtre courante au profil ; afficher dans l’UI un indicateur “proche de la baseline” / “écart fort”.

### Corrélations et contexte

- **Conversations** : grouper les paquets par (src_ip, dst_ip, src_port, dst_port, protocol) pour identifier des “flows” ; durée, nombre de paquets, octets. Utile pour “qui a parlé à qui, combien de temps”.
- **Services ↔ IPs** : croiser `service_info` (HTTPS, DNS, etc.) avec `top_src_ips` / `top_dst_ips` pour étiqueter “cette IP = surtout HTTPS” ou “cette IP = DNS récursif”.
- **Subnets** : utiliser `src_network` (Silver/Gold) pour rapports par segment (ex. 192.168.1.0/24 vs 10.0.0.0/24) ; comparer volumes et top ports par subnet.

### Export et reporting

- **Export CSV/Excel** : partir de `_live.json` (recent_packets) ou de Silver Parquet pour générer des rapports “trafic des N dernières heures” (par IP, port, service) et les partager.
- **Rapports périodiques** : job (cron / Task Scheduler) qui lit Silver ou Gold, agrège par jour/semaine, produit un résumé (volume, top IPs, top ports, évolution) en HTML/PDF ou en JSON pour un outil BI.
- **API d’agrégats** : endpoint REST (ex. `/api/stats?window=1h`) qui lit `_live.json` ou Silver et renvoie des agrégats (protocols, top IPs, port_stats) pour intégration dans un autre dashboard ou outil.

### Détection d’anomalies (idées avancées)

- **Volume par (IP, port)** : seuil dynamique (moyenne + N × écart-type sur des fenêtres passées) ; alerte si dépassement.
- **Ports éphémères** : repérer des connexions depuis des ports hauts (> 32768) vers des services (443, 80) en grand nombre → possible scan ou bot.
- **TTL** : distribution des TTL (déjà dans Silver) ; une forte proportion de TTL très bas ou très homogène peut indiquer un type de trafic (ex. traceroute, certains C2).

### Réutilisation des fichiers existants

| Donnée | Fichier / source | Idée d’exploitation |
|--------|-------------------|---------------------|
| Paquets récents | `_live.json` → `recent_packets` | Export, recherche full-text, corrélation avec un flow. |
| Agrégats temps réel | `_live.json` → protocols, top_src_ips, port_stats, protocol_windows | Alertes, comparaison fenêtre courante vs précédente. |
| Santé capture | `_live.json` → `capture_health` | Alerting drop rate, graphique tendance. |
| Historique structuré | Silver / Gold Parquet | BI (Metabase, Power BI, DuckDB), ML (features par heure/IP), conformité (qui a communiqué quand). |

En ajoutant des **jobs légers** (script Python ou petit service) qui lisent `_live.json` et Silver/Gold, on peut implémenter alertes, baselines et exports sans modifier le cœur capture/pipeline.

---

## Project Structure

**EN** — Package root is `pktlive/`; all application code lives under `src/network_interface/`. Entry points: `run.py` (capture + TUI) and `src.network_interface.web.web_main` (web UI).

**FR** — Racine du projet : `pktlive/` ; tout le code applicatif est sous `src/network_interface/`. Points d’entrée : `run.py` (capture + TUI) et `src.network_interface.web.web_main` (UI web).

```
pktlive/
├── run.py                    # Unified launcher (capture + pipeline + optional TUI)
├── requirements.txt
├── pytest.ini
├── setup.ps1                 # Windows bootstrap
├── setup.sh                  # Linux bootstrap
│
├── src/
│   ├── __init__.py
│   └── network_interface/
│       ├── __init__.py
│       ├── _paths.py         # Project root, bronze/silver/gold paths
│       ├── run_main.py       # Main process: capture thread, parser pool, DataSink spawn
│       │
│       ├── capture/          # Capture engine + DataSink process (Bronze/Silver/Gold)
│       │   ├── capture_agent.py   # NIC → queue → DataSink; Npcap/AF_PACKET backends
│       │   └── network_collector.py
│       │
│       ├── monitoring/       # Observability
│       │   ├── structured_log.py   # JSON logging (Splunk/ELK/Loki ready)
│       │   ├── pipeline_metrics.py  # Real-time _metrics.json
│       │   └── dashboard.py        # htop-style TUI (Live/Ports/Stats)
│       │
│       ├── storage/          # Medallion layers
│       │   ├── bronze_store.py     # Bronze NDJSON (partition, manifest, quarantine)
│       │   ├── streaming_pipeline.py  # Silver/Gold transforms, compaction, retention
│       │   └── medallion_core.py   # Schema / Parquet helpers
│       │
│       ├── web/              # FastAPI dashboard
│       │   ├── web_main.py   # Entry: start capture if needed, serve UI on :8000
│       │   ├── app.py        # API + routes, static/templates
│       │   ├── static/       # app.js, styles.css
│       │   └── templates/    # dashboard.html, fragments (live, ports, stats, pipeline)
│       │
│       └── cloud/            # Optional cloud pipeline
│           └── databricks_pipeline.py  # Local Parquet reader + Spark/Delta
│
├── tests/                    # 104 tests, <2s, pytest --cov=src
│   ├── conftest.py
│   ├── test_silver_transform.py
│   ├── test_stores.py
│   ├── test_bronze_store.py
│   ├── test_packet_parser.py
│   ├── test_packet_record.py
│   ├── test_pipeline_integration.py
│   ├── test_compaction_retention.py
│   ├── test_structured_logging.py
│   ├── test_capture_backends.py
│   ├── test_web_ui.py
│   ├── test_fuzz_silver.py
│   ├── test_resilience.py
│   └── test_performance_smoke.py
│
├── scripts/
│   ├── run.py, run.cmd, run.ps1, run   # Launcher shortcuts
│   └── bundle.py                       # Build/bundle helpers
│
├── docs/                     # Additional documentation
│
├── bronze/                   # [generated] Raw packet NDJSON
│   ├── data/dt=YYYY-MM-DD/hr=HH/*.ndjson
│   ├── _manifest/manifest.jsonl
│   ├── _quarantine/
│   └── _meta/agent.json
│
├── silver/                   # [generated] Cleaned Parquet
│   └── event_date=YYYY-MM-DD/*.parquet
│
├── gold/                     # [generated] Aggregated Parquet
│   └── event_date=YYYY-MM-DD/*.parquet
│
├── _live.json                # [generated] Dashboard snapshot (300ms refresh)
├── _metrics.json             # [generated] Pipeline health metrics (2s refresh)
└── _metrics_capture.json     # [generated] Capture-side metrics
```

---

## Technology Stack

```mermaid
flowchart LR
    subgraph CAPTURE["Capture"]
        A["AF_PACKET / Npcap"]
        B["dpkt"]
    end

    subgraph PIPELINE["Pipeline"]
        C["Pydantic v2"]
        D["PyArrow"]
    end

    subgraph STORAGE["Storage"]
        E["NDJSON"]
        F["Parquet Snappy"]
    end

    subgraph OBS["Observability"]
        G["JSON Logging"]
        H["Metrics JSON"]
    end

    CAPTURE --> PIPELINE
    PIPELINE --> STORAGE

    style CAPTURE fill:#e17055,stroke:#d63031,color:#fff
    style PIPELINE fill:#0984e3,stroke:#74b9ff,color:#fff
    style STORAGE fill:#00b894,stroke:#55efc4,color:#fff
    style OBS fill:#6c5ce7,stroke:#a29bfe,color:#fff
```

| Layer | Technology | Role |
|---|---|---|
| Capture (Linux) | **AF_PACKET** raw socket | Kernel-native, zero-copy, GIL released |
| Capture (Windows) | **Npcap** via `ctypes` | Direct C binding to `wpcap.dll` |
| Parsing | **dpkt** + `ThreadPoolExecutor` | Struct-based, 130x faster than Scapy |
| I/O Sink | **multiprocessing.Process** | Dedicated process, bypasses GIL |
| Validation | **Pydantic v2** | Schema enforcement on every record |
| NIC Detection | **psutil** | Auto-detect most-active NIC |
| Bronze | **NDJSON** | Partitioned, manifest-tracked, compacted |
| Silver / Gold | **PyArrow** Parquet | In-memory transform + columnar writes |
| Logging | **JSON structured** | Splunk / ELK / Loki compatible |
| Metrics | **Atomic JSON** | packets/sec, latency p50/p95/p99, queue depth |
| Tests | **pytest** | 62 tests, < 1 second |
| Cloud (optional) | **PySpark** + **Delta Lake** | Auto Loader on Databricks |

---

## Dependencies

### Edge Agent & Pipeline (local, no JVM)

| Package | Version | Role |
|---------|---------|------|
| `dpkt` | `>=1.9.8` | Ethernet/IP/TCP/UDP parsing |
| `pydantic` | `>=2.10.0` | Record schema validation |
| `psutil` | `>=6.1.0` | NIC detection |
| `pyarrow` | `>=15.0.0` | Silver/Gold Parquet I/O |
| `fastapi` | `>=0.115.0` | Web UI API |
| `uvicorn` | `>=0.30.0` | ASGI server |
| `jinja2` | `>=3.1.0` | Web UI templates |

### Tests

| Package | Version |
|---------|---------|
| `pytest` | `>=8.0.0` |
| `hypothesis` | `>=6.0.0` |
| `httpx` | `>=0.27.0` |

### System Prerequisites

| Component | OS | Notes |
|-----------|-----|------|
| **Npcap** | Windows | [npcap.com](https://npcap.com/#download) — WinPcap API-compatible mode |
| **Root / CAP_NET_RAW** | Linux | Required for AF_PACKET raw socket |

---

## Data Schemas

### Bronze (NDJSON)

```json
{
  "timestamp": "2026-02-25T14:32:01.123456+00:00",
  "src_ip": "192.168.1.42",
  "dst_ip": "142.250.74.206",
  "src_port": 54321,
  "dst_port": 443,
  "protocol": "TCP",
  "length": 1420,
  "ttl": 64,
  "flags": "PA",
  "agent_id": "a1b2c3d4e5f6"
}
```

### Silver (Parquet)

| Column | Type | Source |
|---|---|---|
| `event_ts` | `TIMESTAMP(us, UTC)` | Parsed from `timestamp` |
| `event_hour` | `TIMESTAMP(us, UTC)` | Truncated to hour |
| `event_date` | `DATE32` | Partition key |
| `src_ip` | `STRING` | Passthrough |
| `dst_ip` | `STRING` | Passthrough |
| `src_port` | `INT32` | Passthrough |
| `dst_port` | `INT32` | Passthrough |
| `protocol` | `STRING` | Uppercased |
| `length` | `INT64` | Passthrough |
| `ttl` | `INT32` | Passthrough |
| `flags` | `STRING` | Passthrough |
| `agent_id` | `STRING` | Passthrough |
| `ingested_at` | `TIMESTAMP(us, UTC)` | Pipeline ingestion time |
| `src_network` | `STRING` | CIDR /24 (`192.168.1.0/24`) |

### Gold (Parquet — hourly aggregates)

| Column | Type |
|---|---|
| `event_hour` | `TIMESTAMP(us, UTC)` |
| `event_date` | `DATE32` — partition key |
| `protocol` | `STRING` |
| `src_network` | `STRING` |
| `agent_id` | `STRING` |
| `packet_count` | `INT64` |
| `total_bytes` | `INT64` |
| `avg_packet_size` | `FLOAT64` |
| `first_seen` | `TIMESTAMP(us, UTC)` |
| `last_seen` | `TIMESTAMP(us, UTC)` |
| `unique_src_ips` | `INT64` |
| `unique_dst_ips` | `INT64` |
| `unique_dst_ports` | `INT64` |
| `computed_at` | `TIMESTAMP(us, UTC)` |

---

## Latency Targets

```mermaid
flowchart LR
    subgraph BUDGET["Latency Budget"]
        A["Capture to Queue<br/>< 1 ms"]
        B["Queue to Bronze<br/>< 10 ms"]
        C["Silver flush<br/>< 500 ms"]
        D["Gold flush<br/>< 1 s"]
        E["_live.json read<br/>< 1 ms"]
    end
    A --> B --> C --> D
    A -.-> E

    style BUDGET fill:#2d3436,stroke:#636e72,color:#dfe6e9
```

| Stage | Target | Mechanism |
|---|---|---|
| Capture -> Queue | **< 1 ms** | OS-native recv + `put_nowait` |
| Queue -> Bronze disk | **< 10 ms** | Buffered NDJSON write |
| Queue -> Silver disk | **< 500 ms** | In-memory transform + PyArrow flush (500 records or 0.5s) |
| Queue -> Gold disk | **< 1 s** | In-memory accumulation + PyArrow flush (1s) |
| Dashboard data load | **< 1 ms** | Atomic JSON read (`_live.json`) |

---

## Design Decisions

### Why inline streaming instead of Spark local?

| Concern | Spark batch | Inline streaming |
|---|---|---|
| Latency | 5-30s (JVM startup + planning) | **< 1 second** |
| Layer transport | File-based (write, seal, re-read) | **In-memory** (same process, same tick) |
| JVM dependency | Required (Java 11+) | **None** |
| Memory overhead | ~500 MiB (Spark driver) | **~50 MiB** (Python + PyArrow) |

### Why dpkt instead of Scapy?

| Metric | Scapy | dpkt |
|---|---|---|
| Parse 100k packets | 29.9 sec | **0.23 sec** (130x) |
| RAM for 100k packets | 678.9 MiB | **19.7 MiB** (34x) |
| Dependencies | ~15 packages | 0 (standalone) |

### Crash Safety

```mermaid
sequenceDiagram
    participant U as User
    participant M as Main Process
    participant S as DataSink Process

    U->>M: SIGINT
    M->>M: shutdown_event set
    M->>M: capture_thread join
    M->>M: backend close
    M->>S: mp.Event
    S->>S: Drain queue
    S->>S: Flush Silver Gold Snapshot
    S->>S: fsync seal Bronze
    S->>M: Process exits
    M->>M: Shutdown complete
```

---

## Tests (detailed) / Tests (détail)

**EN** — 104 tests covering pipeline, storage, parsing, logging, and integration. Execution **< 2 s**. See [Test coverage](#test-coverage--couverture-des-tests) for coverage report and `pytest --cov=src`.

**FR** — 104 tests couvrant pipeline, stockage, parsing, logging et intégration. Exécution **< 2 s**. Voir [Couverture des tests](#test-coverage--couverture-des-tests) pour le rapport et `pytest --cov=src`.

```bash
python -m pytest tests/ -v
python -m pytest tests/ --cov=src --cov-report=term-missing -v   # with coverage / avec couverture
```

| Test File | Tests | Coverage |
|---|---|---|
| `test_silver_transform.py` | 13 | Quality gate, timestamps, subnets, defaults |
| `test_stores.py` | 10 | SilverStore, GoldAccumulator, LiveSnapshot |
| `test_bronze_store.py` | 7 | Write, rotation, quarantine, manifest |
| `test_packet_parser.py` | 7 | dpkt TCP/UDP parsing, flags, edge cases |
| `test_compaction_retention.py` | 8 | Parquet compaction, schema evolution, retention |
| `test_structured_logging.py` | 8 | JSON formatter, correlation ID, metrics |
| `test_pipeline_integration.py` | 2 | End-to-end Bronze → Silver → Gold → Snapshot |

---




