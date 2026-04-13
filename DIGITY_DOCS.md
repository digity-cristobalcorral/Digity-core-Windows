# Digity — Documentación del sistema

## Índice
1. [Arquitectura general](#1-arquitectura-general)
2. [Estructura de archivos](#2-estructura-de-archivos)
3. [Componentes del sistema](#3-componentes-del-sistema)
4. [Flujo de datos completo](#4-flujo-de-datos-completo)
5. [Protocolo HUMI (formato binario del sensor)](#5-protocolo-humi)
6. [Sistema de grabación](#6-sistema-de-grabación)
7. [Dashboard web](#7-dashboard-web)
8. [Integración con Unity desde cero](#8-integración-con-unity-desde-cero)
9. [Integración con ROS2 e Isaac Sim](#9-integración-con-ros2-e-isaac-sim)

---

## 1. Arquitectura general

```
Hardware ESP32 (sensores)
    │  ESP-NOW (radio 2.4 GHz)
    ▼
ESP32 Gateway (USB /dev/ttyUSB0)
    │  Serial 921600 baud — bytes binarios HUMI
    ▼
┌──────────────────────────────────────────────────────┐
│  DIGITY (Python — /home/digity/glove-core/)          │
│                                                      │
│  ┌───────────────┐    UDP :9002    ┌───────────────┐ │
│  │ exo_capture   │ ─────────────► │ zmq_publisher │ │
│  │ (serial read) │                │               │ │
│  │  + parser     │                │  ZMQ PUB      │ │
│  │  + recorder   │                │  :5555        │ │
│  └───────────────┘                └───────────────┘ │
│                                          │           │
│  ┌───────────────┐    UDP :5005          │           │
│  │ station_daemon│ ◄── Dashboard         │           │
│  │ (coordinator) │                       │           │
│  └───────────────┘                       │           │
│                                          │           │
│  ┌───────────────────────────────────┐   │           │
│  │ Flask + SocketIO (Dashboard :5000)│   │           │
│  └───────────────────────────────────┘   │           │
└──────────────────────────────────────────┼───────────┘
                                           │
                              tcp://IP:5555
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
                 Unity           ROS2          Isaac Sim
```

---

## 2. Estructura de archivos

```
glove-core/
├── main.py                    # Entry point (--web o --app)
├── core/
│   ├── config.py              # Todos los puertos y rutas centralizados
│   ├── humi_protocol.py       # Parser del protocolo binario HUMI
│   ├── zmq_publisher.py       # Servicio ZMQ PUB (reenvía frames a Unity/ROS2)
│   └── service_manager.py     # Gestión de subprocesos (start/stop/restart)
├── producer/
│   └── exo_capture.py         # Lee serial, graba .raw, parsea y envía por UDP
├── app/
│   ├── server.py              # Flask + SocketIO (API REST + WebSocket)
│   ├── station_daemon.py      # Daemon UDP — coordina start/stop de grabación
│   └── templates/
│       ├── dashboard.html     # Dashboard principal
│       └── setup.html         # Página de configuración
└── logs/                      # Logs de servicios
```

**Datos grabados:**
```
/mnt/data/session/
└── <user>_<session>_<station>/
    ├── sensors/
    │   └── stream.raw         # Bytes binarios HUMI exactos del serial
    └── info/
        └── exo_info.json      # Metadatos: host_ts_start, host_ts_end, paths
```

---

## 3. Componentes del sistema

### 3.1 `exo_capture.py` — Capturador serial

- **Lee bytes en bruto** del ESP32 gateway via `/dev/ttyUSB0` a 921600 baud, **siempre** (sin importar si hay grabación activa).
- **Cuando RECORDING=True**: escribe los bytes directamente a `stream.raw` (append mode, buffer 4MB).
- **Siempre**: parsea los bytes con `humi_protocol.parse_stream()` y envía cada frame parseado como JSON via UDP al puerto 9002.
- **Siempre**: envía telemetría (estadísticas: bytes/s, recording flag) via UDP al puerto 9002.
- Escucha comandos `record_start` / `record_stop` en UDP :9052.

### 3.2 `humi_protocol.py` — Parser HUMI

Convierte bytes binarios del ESP32 en dicts Python.

Entrada: stream de bytes (buffer acumulado).
Salida: lista de frames parseados + bytes sobrantes.

Cada frame tiene forma:
```json
{
  "side": "right",
  "group": "arm",
  "node_id": 1,
  "seq": 42,
  "sensors": [
    {
      "type": "angles",
      "finger": 0,
      "com": 0,
      "n": 3,
      "samples": [
        {"ts_us": 1000000, "angles_deg": [14.2, 26.8, 35.1]},
        {"ts_us": 1010000, "angles_deg": [14.5, 27.1, 35.4]}
      ]
    },
    {
      "type": "imu6",
      "finger": 0,
      "com": 0,
      "samples": [
        {"ts_us": 980000, "acc": [312, -45, 920], "gyro": [100, -30, 5]}
      ]
    },
    {
      "type": "touch6",
      "finger": 0,
      "com": 0,
      "ts_us": 990000,
      "channels": [0.52, 0.41, 0.67, 0.33, 0.78, 0.25],
      "channels_raw": [2130, 1680, 2745, 1352, 3194, 1024]
    }
  ]
}
```

### 3.3 `zmq_publisher.py` — Publicador ZMQ

- Escucha UDP :9002 (mismo puerto que usa `exo_capture` para enviar).
- Enruta por tipo:
  - `{"type": "sensor_frame", "frame": {...}}` → publica en topic ZMQ **`"sensor"`**
  - `{"type": "exo_raw_telemetry", ...}` → publica en topic ZMQ **`"raw"`**
- Bind: `tcp://0.0.0.0:5555` (accesible desde la red local).

### 3.4 `station_daemon.py` — Coordinador

- Escucha comandos en UDP :5005 (enviados desde el Dashboard).
- Al recibir `{"cmd": "start", "meta": {...}}`:
  - Genera un `host_ts_start` único para la sesión.
  - Hace fan-out a todos los productores (exo, cámaras) con el mismo timestamp.
- Al recibir `{"cmd": "stop"}`: envía `record_stop` a todos los productores.

### 3.5 `server.py` — API Flask

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| POST | `/start` | Inicia grabación |
| POST | `/stop` | Para grabación |
| GET | `/services` | Estado de todos los servicios |
| GET | `/services/<key>/logs` | Últimas N líneas de log |
| POST | `/services/<key>/start` | Inicia un servicio |
| POST | `/services/<key>/stop` | Para un servicio |
| POST | `/services/<key>/restart` | Reinicia un servicio |
| GET | `/devices` | Estado de hardware (EXO, cámaras) |
| GET | `/session?path=` | Lista archivos en `/mnt/data/session` |
| GET | `/session/download?path=` | Descarga un archivo |
| GET | `/session/preview?path=` | Preview de texto/JSON |
| POST | `/session/delete` | Borra archivo o carpeta |

---

## 4. Flujo de datos completo

### 4.1 En tiempo real (siempre activo)

```
ESP32 → serial bytes → exo_capture → humi_protocol.parse_stream()
    → {type:"sensor_frame", frame:{...}} → UDP :9002
    → zmq_publisher → ZMQ topic "sensor" → tcp://0.0.0.0:5555
```

El ZMQ topic `"sensor"` contiene el frame wrapper completo:
```json
{
  "type": "sensor_frame",
  "frame": { "side":"right", "group":"arm", "node_id":1, "seq":42, "sensors":[...] },
  "ts": 1700000000.123,
  "source": "glove-core"
}
```

### 4.2 Durante grabación

```
ESP32 → serial bytes → exo_capture → RAW_FH.write(bytes) → stream.raw
```

Los bytes se escriben **sin parsear** — exactamente como llegan del serial.

### 4.3 Inicio de grabación

```
Dashboard (browser)
  POST /start {user_id, session_id, task_type, ...}
    → UDP :5005 → station_daemon
      → genera host_ts_start
      → UDP :9052 → exo_capture {cmd:"record_start", host_ts_start, ...}
        → crea /mnt/data/session/<nombre>/
        → abre stream.raw
        → escribe info/exo_info.json
        → RECORDING = True
```

---

## 5. Protocolo HUMI

### Packet header (9 bytes, little-endian)

| Offset | Tipo | Campo | Valor |
|--------|------|-------|-------|
| 0 | u8 | pkt_type | 0x01 |
| 1 | u8 | version | 0x02 |
| 2 | u8 | side | 0=derecha, 1=izquierda |
| 3 | u8 | group | 0=arm, 1=hand |
| 4 | u8 | node_id | arm: 1-4, hand: 11-13 |
| 5-6 | u16 LE | seq | número de secuencia |
| 7-8 | u16 LE | payload_len | longitud del payload |

### Payload = `[n_sens:u8]` + registros de sensores

#### Registro ANGLES (0x10)
```
[sens_typ:u8, sens_id:u8, n_samples:u8, t0_us:u64, dt_us:u16]
+ n_samples × nAngles × i16  (centidegrees → dividir entre 100)
```

`nAngles` según nodo:
- ARM node 1,4: 3 ángulos
- ARM node 2,3: 2 ángulos
- HAND nodes 11,12,13: 5 ángulos

#### Registro IMU6 (0x11)
```
[sens_typ:u8, sens_id:u8, n_samples:u8, t0_us:u64, dt_ax:u16, dt_ay:u16, dt_az:u16]
+ n_samples × (ax:i16, ay:i16, az:i16, gx:i16, gy:i16, gz:i16)
```

#### Registro TOUCH6 (0x12)
```
[sens_typ:u8, sens_id:u8, n_samples:u8, t0_us:u64]
+ 6 × u16  (ADC 0..4095 → dividir entre 4095 para obtener 0..1)
```

#### sens_id encoding
```
bits[7:4] = finger_idx (0..15)
bits[3:0] = com_line   (0..15)
```

---

## 6. Sistema de grabación

### Archivos generados por sesión

```
/mnt/data/session/anon_2024_01_15T10_30_00_123_station1/
├── sensors/
│   └── stream.raw          # Bytes HUMI binarios exactos
└── info/
    └── exo_info.json       # Timestamps de sincronización
```

**exo_info.json:**
```json
{
  "host_ts_start": 1705312200.123,
  "host_ts_end":   1705312260.456,
  "rec_meta": {
    "user_id": "anon",
    "session_id": "2024_01_15T10_30_00_123",
    "task_type": "default",
    "station_id": "station1"
  },
  "raw_path": "/mnt/data/session/.../sensors/stream.raw"
}
```

### Re-parsear un archivo grabado

```python
from core.humi_protocol import parse_stream

with open("sensors/stream.raw", "rb") as f:
    data = f.read()

frames, leftover = parse_stream(data)
for frame in frames:
    print(frame["group"], frame["node_id"], len(frame["sensors"]))
```

---

## 7. Dashboard web

Acceso: `http://localhost:5000`

### Vistas

- **Dashboard**: estado de servicios, control de grabación, logs en vivo, preview de cámaras.
- **Sessions**: explorador de archivos de `/mnt/data/session` (navegar, descargar, previsualizar, borrar).
- **Setup**: `http://localhost:5000/setup`

### Arrancar el sistema

```bash
cd /home/digity/glove-core
source .venv/bin/activate
python main.py          # modo web (navegador)
python main.py --app    # modo app (ventana nativa, requiere GTK)
```

### Variables de entorno relevantes

| Variable | Default | Descripción |
|----------|---------|-------------|
| `GLOVE_SERIAL_PORT` | `/dev/ttyUSB0` | Puerto serial del gateway |
| `GLOVE_BAUD` | `921600` | Velocidad serial |
| `GLOVE_DATA_DIR` | `/mnt/data` | Directorio base de grabaciones |
| `GLOVE_DASHBOARD_PORT` | `5000` | Puerto del dashboard |

---

## 8. Integración con Unity desde cero

### 8.1 Requisitos

- Unity 2021.3 LTS o superior (probado con Unity 6)
- NuGetForUnity (instalador de paquetes NuGet)
- NetMQ 4.x + AsyncIO (dependencia de NetMQ)
- Newtonsoft.Json (para deserialización robusta de JSON)

### 8.2 Instalación de dependencias

**Paso 1 — Instalar NuGetForUnity**

1. En Unity: `Window → Package Manager`
2. Click `+` → `Add package from git URL`
3. Pegar: `https://github.com/GlitchEnzo/NuGetForUnity.git?path=/src/NuGetForUnity`
4. Click `Add`

**Paso 2 — Instalar paquetes NuGet**

1. `NuGet → Manage NuGet Packages`
2. Buscar e instalar (en este orden):
   - `AsyncIO` (versión 0.1.69 o superior)
   - `NetMQ` (versión 4.0.1.12 o superior)
   - `Newtonsoft.Json` (versión 13.x)

### 8.3 Crear el script receptor

**Crear archivo** `Assets/Scripts/DigityReceiver.cs`:

```csharp
using System;
using System.Collections.Generic;
using System.Threading;
using UnityEngine;
using NetMQ;
using NetMQ.Sockets;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

/// <summary>
/// Recibe frames de sensores del sistema Digity via ZMQ y los expone
/// al resto de Unity a través de eventos y propiedades públicas.
/// </summary>
public class DigityReceiver : MonoBehaviour
{
    [Header("Conexión")]
    public string serverIP   = "127.0.0.1";  // IP de la máquina con Digity
    public int    serverPort = 5555;

    [Header("Filtro de topic")]
    [Tooltip("'sensor' para frames parseados | 'raw' para telemetría")]
    public string topic = "sensor";

    [Header("Estado")]
    public bool  isConnected = false;
    public float framesPerSecond = 0f;

    // ── Último frame recibido (hilo-seguro) ─────────────────────────────────
    public SensorFrameWrapper LastFrame { get; private set; }
    public event Action<SensorFrameWrapper> OnFrame;

    // ── Internos ─────────────────────────────────────────────────────────────
    private Thread         _thread;
    private SubscriberSocket _socket;
    private bool           _running;
    private readonly Queue<SensorFrameWrapper> _queue = new Queue<SensorFrameWrapper>();
    private readonly object _queueLock = new object();
    private int   _frameCount;
    private float _fpsTimer;

    void Start()
    {
        AsyncIO.ForceDotNet.Force();
        _running = true;
        _thread = new Thread(RecvLoop) { IsBackground = true };
        _thread.Start();
    }

    void Update()
    {
        // Despachar frames recibidos en el hilo Unity (main thread)
        lock (_queueLock)
        {
            while (_queue.Count > 0)
            {
                var wrapper = _queue.Dequeue();
                LastFrame = wrapper;
                OnFrame?.Invoke(wrapper);
            }
        }

        // FPS counter
        _fpsTimer += Time.deltaTime;
        if (_fpsTimer >= 1f)
        {
            framesPerSecond = _frameCount / _fpsTimer;
            _frameCount = 0;
            _fpsTimer   = 0f;
        }
    }

    void OnDestroy()
    {
        _running = false;
        _socket?.Close();
        NetMQConfig.Cleanup(false);
    }

    // ── Hilo receptor ────────────────────────────────────────────────────────
    void RecvLoop()
    {
        string addr = $"tcp://{serverIP}:{serverPort}";
        try
        {
            using (_socket = new SubscriberSocket())
            {
                _socket.Connect(addr);
                _socket.Subscribe(topic);
                isConnected = true;
                Debug.Log($"[Digity] Connected to {addr} | topic '{topic}'");

                while (_running)
                {
                    // Recibir en dos partes: topic + payload
                    if (!_socket.TryReceiveFrameString(TimeSpan.FromMilliseconds(500), out string topicStr))
                        continue;

                    if (!_socket.TryReceiveFrameString(TimeSpan.FromMilliseconds(100), out string json))
                        continue;

                    try
                    {
                        var wrapper = JsonConvert.DeserializeObject<SensorFrameWrapper>(json);
                        if (wrapper?.frame != null)
                        {
                            lock (_queueLock)
                            {
                                _queue.Enqueue(wrapper);
                                _frameCount++;
                            }
                        }
                    }
                    catch (Exception ex)
                    {
                        Debug.LogWarning($"[Digity] Parse error: {ex.Message}");
                    }
                }
            }
        }
        catch (Exception ex)
        {
            Debug.LogError($"[Digity] Connection error: {ex.Message}");
        }
        finally
        {
            isConnected = false;
        }
    }
}

// ── Estructuras de datos ─────────────────────────────────────────────────────

[Serializable]
public class SensorFrameWrapper
{
    public string      type;    // "sensor_frame"
    public SensorFrame frame;
    public double      ts;      // timestamp Unix del servidor
    public string      source;  // "glove-core"
}

[Serializable]
public class SensorFrame
{
    public string            side;     // "right" | "left"
    public string            group;    // "arm" | "hand"
    public int               node_id;  // arm: 1-4 | hand: 11-13
    public int               seq;
    public List<SensorRecord> sensors;
}

[Serializable]
public class SensorRecord
{
    public string      type;    // "angles" | "imu6" | "touch6"
    public int         finger;  // finger_idx (bits[7:4] del sens_id)
    public int         com;     // com_line   (bits[3:0] del sens_id)
    public int         n;       // nAngles (solo en type=="angles")

    // type == "angles" | "imu6"
    public List<AngleSample> samples;

    // type == "touch6"
    public double          ts_us;
    public List<double>    channels;      // 0.0 .. 1.0
    public List<int>       channels_raw;  // 0 .. 4095
}

[Serializable]
public class AngleSample
{
    public double       ts_us;

    // type == "angles"
    public List<double> angles_deg;

    // type == "imu6"
    public List<int>    acc;   // [ax, ay, az]
    public List<int>    gyro;  // [gx, gy, gz]
}
```

### 8.4 Usar el receptor en una escena

**Opción A — Leer datos en otro script:**

```csharp
public class GloveVisualizer : MonoBehaviour
{
    public DigityReceiver receiver;

    void OnEnable()
    {
        receiver.OnFrame += HandleFrame;
    }

    void OnDisable()
    {
        receiver.OnFrame -= HandleFrame;
    }

    void HandleFrame(SensorFrameWrapper wrapper)
    {
        var frame = wrapper.frame;

        foreach (var sensor in frame.sensors)
        {
            if (sensor.type == "angles" && sensor.samples != null)
            {
                foreach (var sample in sensor.samples)
                {
                    // sample.angles_deg contiene los ángulos en grados
                    // frame.group == "arm"  → hasta 3 ángulos por nodo
                    // frame.group == "hand" → 5 ángulos por dedo
                    Debug.Log($"Node {frame.node_id} finger {sensor.finger}: " +
                              string.Join(", ", sample.angles_deg));
                }
            }

            if (sensor.type == "imu6" && sensor.samples != null)
            {
                foreach (var sample in sensor.samples)
                {
                    var acc  = new Vector3(sample.acc[0],  sample.acc[1],  sample.acc[2]);
                    var gyro = new Vector3(sample.gyro[0], sample.gyro[1], sample.gyro[2]);
                    Debug.Log($"IMU acc={acc} gyro={gyro}");
                }
            }

            if (sensor.type == "touch6" && sensor.channels != null)
            {
                // sensor.channels[0..5] → presión 0.0..1.0 por canal
                float touch0 = (float)sensor.channels[0];
            }
        }
    }
}
```

**Opción B — Acceder al último frame directamente:**

```csharp
void Update()
{
    if (receiver.LastFrame == null) return;

    var frame = receiver.LastFrame.frame;
    // usar frame.sensors...
}
```

### 8.5 Setup en el editor Unity

1. Crear un GameObject vacío → renombrar a `DigityManager`
2. Añadir componente `DigityReceiver`
3. Configurar:
   - `Server IP`: IP de la máquina con Digity (o `127.0.0.1` si es la misma)
   - `Server Port`: `5555`
   - `Topic`: `sensor`
4. En tu script visual, arrastar `DigityManager` al campo `receiver`

### 8.6 Verificar que funciona

Antes de abrir Unity, comprobar que Digity está publicando:

```bash
# En la máquina con Digity
cd /home/digity/glove-core
source .venv/bin/activate

# Test rápido con Python — debe imprimir frames cada ~20ms
python3 -c "
import zmq, json, time
ctx = zmq.Context()
sub = ctx.socket(zmq.SUB)
sub.connect('tcp://127.0.0.1:5555')
sub.setsockopt_string(zmq.SUBSCRIBE, 'sensor')
print('Esperando frames...')
while True:
    topic, data = sub.recv_multipart()
    frame = json.loads(data)
    f = frame['frame']
    print(f'group={f[\"group\"]} node={f[\"node_id\"]} sensors={len(f[\"sensors\"])}')
"
```

### 8.7 Referencia rápida de datos por grupo

| Grupo | Nodos | Tipo sensor | Datos |
|-------|-------|-------------|-------|
| arm | 1, 4 | angles | 3 ángulos por sample (2 samples/pkt) |
| arm | 2, 3 | angles | 2 ángulos por sample (2 samples/pkt) |
| hand | 11, 12, 13 | angles | 5 ángulos por sample (2 samples/pkt) |
| arm + hand | todos | imu6 | acc[3] + gyro[3] por sample (4 samples/pkt) |
| arm + hand | todos | touch6 | 6 canales 0..1 (1 sample/pkt) |

**Tasas de muestreo en el mockup ESP32:**
- Ángulos: 100 Hz (dt = 10 ms)
- IMU: 200 Hz (dt = 5 ms)
- Touch: 50 Hz (dt = 20 ms)
- Paquetes por nodo: 50 pkt/s

### 8.8 Troubleshooting Unity

**No llegan datos:**
- Verificar que `zmq_publisher` está corriendo en el dashboard (estado verde)
- Verificar firewall: abrir puerto TCP 5555 en la máquina Digity
- En Windows con Unity: puede requerir ejecutar Unity como administrador la primera vez

**`DllNotFoundException: libzmq`:**
- Los DLLs de NetMQ deberían estar en `Assets/Packages/NetMQ.x.x.x/`
- Si falta, reinstalar NetMQ desde NuGet

**`InvalidOperationException: AsyncIO not initialized`:**
- Confirmar que `AsyncIO.ForceDotNet.Force()` está en `Start()` ANTES de crear el socket

**Frames llegan pero JSON no deserializa:**
- Verificar que Newtonsoft.Json está instalado (no usar `JsonUtility` de Unity — no soporta listas genéricas)

**Frames muy retrasados:**
- Normal: ZMQ PUB/SUB tiene latencia ~1ms en LAN. Si hay retardo mayor, verificar que no hay buffer acumulado — el subscriber debe consumir tan rápido como produce el publisher.

---

## 9. Integración con ROS2 e Isaac Sim

Permite visualizar la mano robot en RViz o Isaac Sim en tiempo real usando los datos del guante.

### Arquitectura

```
glove-core (zmq_publisher.py)
    │  ZMQ PUB tcp://5555  topic="sensor"
    ▼
glove_bridge (ROS2 node)
    │  publica /joint_states a 50 Hz
    ▼
robot_state_publisher (ROS2)
    │  publica /tf tree (cinemática directa desde URDF)
    ▼
RViz / Isaac Sim
```

---

### Paso 1 — Iniciar glove-core

En una terminal normal (fuera del Docker):

```bash
cd /home/digity/glove-core
python3 core/zmq_publisher.py
```

Debe mostrar que está escuchando UDP 9002 y publicando en ZMQ 5555.

---

### Paso 2 — Entrar al Docker ROS2

```bash
docker exec -it <nombre_contenedor> bash
```

Para saber el nombre del contenedor:

```bash
docker ps
```

---

### Paso 3 — Source de ROS2 (obligatorio en cada terminal nueva)

Dentro del Docker:

```bash
source /opt/ros/humble/setup.bash && source /root/ros2_ws/install/setup.bash
```

> **Nota:** Para no tener que escribirlo cada vez, añadirlo al `~/.bashrc` del Docker:
> ```bash
> echo "source /opt/ros/humble/setup.bash && source /root/ros2_ws/install/setup.bash" >> ~/.bashrc
> ```

---

### Paso 4 — Lanzar robot_state_publisher

En una terminal dentro del Docker (con source hecho):

```bash
ros2 run robot_state_publisher robot_state_publisher \
  --ros-args -p robot_description:="$(cat /root/ros2_ws/p50_simplified/p50/converted_commanded.urdf)"
```

Carga el URDF de la mano P50 y empieza a publicar el árbol de transformadas `/tf`.

---

### Paso 5 — Lanzar glove_bridge

En otra terminal del Docker (con source hecho):

```bash
ros2 run glove_core glove_bridge
```

Debe mostrar:
```
[glove_bridge]: ZMQ connected to tcp://127.0.0.1:5555 topic='sensor'
[glove_bridge]: glove_bridge started — ZMQ tcp://127.0.0.1:5555 → /joint_states
```

---

### Paso 6 — Verificar que llegan datos

```bash
ros2 topic echo /joint_states --once
```

Si los joints del thumb e index tienen valores distintos de 0.0, el pipeline funciona correctamente.

```bash
# Ver frecuencia de publicación (debe ser ~50 Hz)
ros2 topic hz /joint_states

# Ver qué nodos están corriendo
ros2 node list

# Ver todos los topics activos
ros2 topic list
```

---

### Paso 7a — Visualizar en RViz

```bash
rviz2
```

Configuración dentro de RViz:
1. **Fixed Frame** → `palm`
2. **Add** → `RobotModel` → Topic: `/robot_description`
3. **Add** → `TF` (opcional, para ver los ejes de cada joint)

---

### Paso 7b — Visualizar en Isaac Sim

**Archivos necesarios:**
- URDF: `/home/digity/converted.urdf` (usa rutas `file:///`, permisos `digity`)
- Los STL están en `/home/digity/p50_simplified/p50/meshes/`

**Importar el robot:**
1. Isaac Sim → `File > Import` → seleccionar `converted.urdf`
2. En el diálogo confirmar la ruta → `Yes`
3. El robot aparece en `/World/p50_autoconverted`

**Conectar con ROS2:**
1. Habilitar extensión: `Window > Extensions` → buscar `ROS2 Bridge` → activar
2. Abrir el editor de OmniGraph: `Window > Visual Scripting > Action Graph`
3. Añadir nodos:
   - `On Playback Tick`
   - `ROS2 Subscribe Joint State` — topic: `/joint_states`
   - `Articulation Controller` — robot prim: `/World/p50_autoconverted`
4. Conectar: `Tick → Subscribe → Controller`
5. Marcar **Subscriber** checkbox en el nodo subscribe
6. Pulsar **Play** (▶) en la barra de Isaac Sim

---

### Archivos clave

| Archivo | Propósito |
|---|---|
| `/root/ros2_ws/src/glove_core/glove_core/glove_bridge.py` | Nodo ROS2 — publica `/joint_states` |
| `/root/ros2_ws/p50_simplified/p50/converted_commanded.urdf` | URDF para RViz (usa `package://mimic_viz`) |
| `/home/digity/converted.urdf` | URDF para Isaac Sim (usa `file:///`) |
| `/root/ros2_ws/src/mimic_viz/meshes/p50/` | STL meshes necesarios para RViz |

---

### Problemas comunes

**`Package 'glove_core' not found`:**
- Falta el source. Ejecutar el Paso 3.

**Joints todos a 0.0:**
- `zmq_publisher.py` no está corriendo. Ejecutar el Paso 1.

**Solo se mueven thumb e index:**
- Comportamiento esperado con el firmware mockup actual. El ESP32 mockup solo envía `finger_idx=0` y `finger_idx=1`. Para los 5 dedos hay que corregir el firmware.

**RViz mano roja y fragmentada:**
- Hay procesos zombie de `robot_state_publisher`. Matar todos y reiniciar:
  ```bash
  pkill -f robot_state_publisher
  # luego volver a ejecutar el Paso 4
  ```

**Isaac Sim: clic en Yes no hace nada al importar URDF:**
- Usar `converted.urdf` (no `converted_commanded.urdf`). El URDF debe usar rutas `file:///` y tener permisos de lectura para el usuario `digity`.
