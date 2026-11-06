 # LNM Sniper v7.3 Dual


**Bot autónomo de trading de Bitcoin en LN Markets** con arquitectura dual (Mainnet + Testnet), aprendizaje por refuerzo (Q-Learning), percepción de corto plazo y protecciones de capital de grado producción.


Diseñado para ejecutarse de forma estable en **Termux** (Android) o cualquier entorno Linux con Python 3.


---


## Resumen del Proyecto


LNM Sniper es un sistema de trading algorítmico orientado a la **acumulación neta de satoshis**. Combina:


    Análisis multi-timeframe (5m / 15m / 1h / 4h)
    Cerebro Q-Learning con 9 acciones discretas
    Motor de percepción en tiempo real (detección de giros en V, breakouts y momentum)
    Gestión de riesgo estricta con piso duro, drawdown máximo y control de exposición
    Separación total de cerebros y credenciales entre Mainnet y Testnet
    Nutrición segura del cerebro de producción a partir de la exploración en Testnet 


El bot opera sobre el mercado de futuros/perpetuos de Bitcoin de **LN Markets** (API v3).


---


## Lógica Principal


### 1. Cerebro (AutonomousGoalBrain)

    Tabla Q con estados discretizados a partir de indicadores técnicos y régimen de mercado.
    9 acciones: `NO_TRADE` + 4 estilos Long + 4 estilos Short (Sniper, Runner, Defensive, etc.).
    Aprendizaje dual:
    **Económico**: recompensa basada en PnL neto en sats (descontando fees reales).
    **Táctico intermedio**: actualización online cada ~3 minutos mientras la operación está abierta.
    Meta dinámica de balance disponible + avance de hitos con bloqueo de piso. 


### 2. Motor de Percepción (`ShortTermPerceptionEngine`)

    Detecta aceleraciones de precio, giros en V y micro-breakouts usando proxies OHLCV de 5m/15m.
    Genera un **Override** temporal (BULL / BEAR) con decaimiento e histéresis.
    El override puede redirigir la acción del cerebro, pero **nunca salta las protecciones P0** en Mainnet. 


### 3. Compuerta de Expectativa de Sats (`SatsExpectancyGate`)

Filtros de seguridad prioritarios (P0):

    Bloqueo de entradas en `RANGE_SQUEEZE + HIGH_EXP`
    Bloqueo de acciones con Q muy negativo
    Verificación de ratio neto TP / Fees ≥ 3.85× (Mainnet)
    Control de exposición proyectada y tamaño máximo de lote 


### 4. Guardian de Piso Duro (`HardFloorGuardian`)

    Piso inicial al 75 % del pico de equity
    Piso de hito al 88 %
    Drawdown máximo 16 % (Mainnet) / 26 % (Testnet)
    Kill-switch automático al 22 % de drawdown 


### 5. Arquitectura Dual

| Entorno | Cerebro | Posiciones máx | Riesgo | Exploración |

|-----------|----------------------------|----------------|------------|-------------|

| Mainnet | `q_brain_advanced_lnm.json`| 5 | Estricto | Baja |

| Testnet | `q_brain_testnet_lnm.json` | 10 | Relajado | Alta |


El demonio de sincronización (`sync_brains_dual.py`) transfiere de forma segura estados de alta confianza desde Testnet → Mainnet cada 30 minutos sin interrumpir los bots en ejecución.


---


## Requisitos


    Python 3.9+
    Sistema con soporte de bloqueos POSIX (`fcntl`) — compatible con Termux
    Conexión a Internet estable 


### Dependencias


```bash

pip install requests numpy

```


Opcional (recomendado en Termux):


```bash

pkg install python

pip install --upgrade pip

```


---


## Instalación y Configuración


    Clona o copia todos los archivos del proyecto en un directorio de trabajo. 


2. Dale permisos de ejecución al script de orquestación:


```bash

chmod +x setup_dual_sniper.sh

```


3. Ejecuta el setup inicial:


```bash

./setup_dual_sniper.sh setup

```


Esto realizará:

    Instalación de dependencias
    Creación de plantilla `.env`
    Inicialización de cerebros
    Ejecución del protocolo de auditoría (15 escenarios) 


4. Edita el archivo `.env` con tus credenciales reales:


```env

# Mainnet (Producción)

LNM_API_KEY=tu_api_key_mainnet

LNM_API_SECRET=tu_api_secret_mainnet

LNM_PASSPHRASE=tu_passphrase_mainnet


# Testnet (Explorador)

LNM_TESTNET_API_KEY=tu_api_key_testnet

LNM_TESTNET_API_SECRET=tu_api_secret_testnet

LNM_TESTNET_PASSPHRASE=tu_passphrase_testnet

```


---


## Uso


```bash

# Iniciar solo Mainnet

./setup_dual_sniper.sh start-main


# Iniciar solo Testnet

./setup_dual_sniper.sh start-test


# Iniciar ambos + demonio de nutrición

./setup_dual_sniper.sh start-both


# Ver estado de procesos

./setup_dual_sniper.sh status


# Detener todo

./setup_dual_sniper.sh stop


# Forzar sincronización de cerebros

./setup_dual_sniper.sh sync

```


### Logs


```bash

tail -f sniper_mainnet.log

tail -f sniper_testnet.log

tail -f sniper_brain_sync.log

```


---


## Archivos del Proyecto


| Archivo | Descripción |

|----------------------------------|--------------------------------------------------|

| `lnm_sniper_v6_advanced.py` | Motor principal del bot |

| `guardian_execution_core.py` | Almacenamiento atómico, locks y cliente HTTP |

| `sniper_perception_sats_core.py` | Percepción de corto plazo + compuerta de sats |

| `sync_brains_dual.py` | Fusión segura Testnet → Mainnet |

| `retrain_sats_offline.py` | Recalibración offline orientada a sats netos |

| `test_protocolo_v_reversal.py` | Suite de 15 escenarios de certificación |

| `setup_dual_sniper.sh` | Orquestador Termux / Linux |


---


## Características de Seguridad


    **Persistencia atómica** con verificación SHA-256 y recuperación automática desde `.bak`
    **Bloqueos POSIX** (`fcntl`) para evitar corrupción entre procesos
    **Circuit Breaker** de API con backoff exponencial
    **Piso duro** y kill-switch de drawdown
    **Filtros anti-churning** de comisiones
    **Separación total** de credenciales y cerebros Mainnet/Testnet
    **Modo Explorer** (Testnet) permite mayor exploración sin comprometer capital real 


---


## Recalibración Offline


Para ajustar la política hacia mayor acumulación de sats netos:


```bash

python3 retrain_sats_offline.py # genera archivo .recalibrated.json

python3 retrain_sats_offline.py --apply # sobrescribe solo si no hay trades activos

```


---


## Advertencia


Este software es experimental y está orientado a usuarios avanzados. El trading de derivados apalancados conlleva un **riesgo elevado de pérdida total del capital**.


    Úsalo primero en **Testnet**.
    Nunca inviertas dinero que no puedas permitirte perder.
    El autor no se hace responsable de pérdidas derivadas del uso de este bot. 


---


**Versión**: 7.3 Dual (Mainnet + Testnet Explorer)

**Enfoque**: Acumulación neta de satoshis con protecciones de capital de producción. 
