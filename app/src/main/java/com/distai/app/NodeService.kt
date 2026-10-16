package com.distai.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/**
 * Servizio in primo piano che tiene vivi i processi nativi:
 *  - worker:       ggml-rpc-server, espone CPU/RAM del telefono sulla LAN (porta 50052)
 *  - coordinatore: llama-server con --rpc verso tutti i worker, API OpenAI su porta 8080
 *  - replica:      llama-server con il modello intero su porta 8081 (modalità repliche)
 *  - router:       smista le richieste sulle repliche, API OpenAI su porta 8080
 */
class NodeService : Service() {

    companion object {
        const val ACTION_WORKER_START = "worker_start"
        const val ACTION_WORKER_STOP = "worker_stop"
        const val ACTION_COORD_START = "coord_start"
        const val ACTION_COORD_STOP = "coord_stop"
        const val ACTION_REPLICA_START = "replica_start"
        const val ACTION_REPLICA_STOP = "replica_stop"
        const val ACTION_ROUTER_START = "router_start"
        const val ACTION_ROUTER_STOP = "router_stop"
        const val EXTRA_HOSTS = "hosts"
        const val EXTRA_MODEL = "model"
        const val EXTRA_RPC = "rpc"
        const val EXTRA_CTX = "ctx"
        const val EXTRA_NGL = "ngl"
        const val EXTRA_TS = "ts"
        const val EXTRA_DRAFT = "draft"
        const val EXTRA_DRAFT_MAX = "draft_max"
        /** esperti attivi per token (MoE veloce); 0 = predefinito del modello */
        const val EXTRA_EXPERTS = "experts"

        const val NSD_TYPE = "_distai._tcp."
        private const val CHANNEL = "distai"
        private const val NOTIF_ID = 1

        fun send(ctx: Context, action: String, extras: Intent.() -> Unit = {}) {
            val i = Intent(ctx, NodeService::class.java).setAction(action).apply(extras)
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i) else ctx.startService(i)
        }
    }

    private var worker: Process? = null
    private var coordinator: Process? = null
    private var replica: Process? = null
    private var router: Router? = null
    private var nsdListener: NsdManager.RegistrationListener? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var multicastLock: WifiManager.MulticastLock? = null

    private val status by lazy { StatusServer(this) }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        SelfBench.load(this)
        status.start()
        Keepalive.start()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        goForeground()
        when (intent?.action) {
            ACTION_WORKER_START -> startWorker()
            ACTION_WORKER_STOP -> stopWorker()
            ACTION_COORD_START -> startCoordinator(
                intent.getStringExtra(EXTRA_MODEL)!!,
                intent.getStringExtra(EXTRA_RPC).orEmpty(),
                intent.getIntExtra(EXTRA_NGL, 0),
                intent.getStringExtra(EXTRA_TS).orEmpty(),
                intent.getIntExtra(EXTRA_CTX, 4096),
                intent.getStringExtra(EXTRA_DRAFT).orEmpty(),
                intent.getIntExtra(EXTRA_DRAFT_MAX, 4),
                intent.getIntExtra(EXTRA_EXPERTS, 0),
            )
            ACTION_COORD_STOP -> stopCoordinator()
            ACTION_REPLICA_START -> startReplica(intent.getStringExtra(EXTRA_MODEL)!!, intent.getIntExtra(EXTRA_CTX, 4096))
            ACTION_REPLICA_STOP -> stopReplica()
            ACTION_ROUTER_START -> startRouter(
                intent.getStringArrayListExtra(EXTRA_HOSTS).orEmpty(), intent.getStringExtra(EXTRA_MODEL).orEmpty())
            ACTION_ROUTER_STOP -> stopRouter()
        }
        stopIfIdle()
        return START_STICKY
    }

    // ---------------------------------------------------------------- worker

    private fun startWorker() {
        if (worker != null) return
        val bin = Native.rpcServer(this)
        val cache = File(cacheDir, "rpc-cache").apply { mkdirs() }
        val cmd = listOf(
            bin.path, "-H", "0.0.0.0", "-p", Native.RPC_PORT.toString(),
            "-t", Native.bigCores().toString(), "-c",
        )
        // threadpool persistente con thread fissati sui core big (patch DistAI in ggml-rpc): sul Note 10 senza
        // maschera i 4 thread finivano su 2 core e il worker andava 4 volte più lento
        val env = mapOf("LLAMA_CACHE" to cache.path) +
            (Native.bigCoreMask()?.let { mapOf("GGML_RPC_CPUMASK" to it) } ?: emptyMap())
        worker = launch("worker", cmd, env) {
            worker = null
            NodeState.workerRunning = false
            WorkerStats.reset()
            unregisterNsd()
            stopIfIdle()
        }
        NodeState.workerRunning = worker != null
        if (worker != null) registerNsd()
        // bench di questo dispositivo all'avvio (worker libero): finisce in /status per tutti gli altri nodi
        if (worker != null) SelfBench.start(this)
        updateNotification()
    }

    private fun stopWorker() {
        worker?.destroy()
        worker = null
        NodeState.workerRunning = false
        unregisterNsd()
        updateNotification()
    }

    /** Annuncia il worker sulla LAN via mDNS, con RAM e CPU nei TXT record. */
    private fun registerNsd() {
        val (total, free) = Native.memInfo(this)
        val info = NsdServiceInfo().apply {
            serviceName = "DistAI ${Native.deviceName()}"
            serviceType = NSD_TYPE
            port = Native.RPC_PORT
            setAttribute("dev", Native.deviceName().take(60))
            setAttribute("ram", total.toString())
            setAttribute("free", free.toString())
            setAttribute("cpu", "${Native.cores()}/${Native.variant}")
        }
        val l = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(si: NsdServiceInfo) = NodeState.log("nsd", "annunciato come ${si.serviceName}")
            override fun onRegistrationFailed(si: NsdServiceInfo, err: Int) = NodeState.log("nsd", "annuncio fallito ($err)")
            override fun onServiceUnregistered(si: NsdServiceInfo) {}
            override fun onUnregistrationFailed(si: NsdServiceInfo, err: Int) {}
        }
        nsdListener = l
        (getSystemService(NSD_SERVICE) as NsdManager).registerService(info, NsdManager.PROTOCOL_DNS_SD, l)
    }

    private fun unregisterNsd() {
        nsdListener?.let { runCatching { (getSystemService(NSD_SERVICE) as NsdManager).unregisterService(it) } }
        nsdListener = null
    }

    // ----------------------------------------------------------- coordinatore

    /**
     * Thread di generazione fissati sui core big. Sui big.LITTLE un thread finito su un core piccolo rallenta tutti
     * a ogni sincronizzazione: Note 10 in throttling da 13-16 a 21 tok/s (misurato 11/10/2026).
     */
    private fun pinBig(): List<String> =
        Native.bigCoreMask()?.let { listOf("--cpu-strict", "1", "-C", it) } ?: emptyList()

    /**
     * Strategia ibrida: i primi layer girano sulla CPU di questo telefono direttamente dal file in mmap,
     * gli ultimi [ngl] layer vanno ai nodi remoti in [rpc], ripartiti secondo [ts].
     */
    private fun startCoordinator(model: String, rpc: String, ngl: Int, ts: String, ctxSize: Int, draft: String, draftMax: Int, experts: Int = 0) {
        if (coordinator != null) return
        val bin = Native.llamaServer(this)
        val remote = if (rpc.isEmpty()) emptyList() else
            // llama.cpp conta il layer di output come un layer in più e lo manda all'ultimo nodo: i logits
            // (vocabolario × 4 byte, ~600 KB/token per Qwen) tornerebbero via Wi-Fi a ogni token (misurato: 3,5 → 5,3 tok/s).
            // Quindi ngl+1 e l'output resta qui: in rete viaggia solo lo stato nascosto (pochi KB).
            listOf("--rpc", rpc, "-ngl", (ngl + 1).toString(),
                "-ot", "^(output|output_norm|token_embd)[.]weight$=CPU",
                // mentre aspetta il worker la CPU si addormenta e riparte lenta: il polling dei thread fissati
                // sui core big porta il coordinatore del Mate 10 Pro da 78 a 56 ms/token (misurato 11/10/2026)
                "--poll", "100") +
                (if (ts.isNotEmpty()) listOf("-ts", ts) else emptyList())
        // modello predittivo (bozza) sulla CPU di questo telefono, mai sui nodi remoti: via RPC costa un giro di rete
        // per ogni token proposto (misurato: bozza sul Huawei via RPC 2,24 tok/s contro 4,5 senza bozza)
        val spec = if (draft.isEmpty()) emptyList() else
            // thread e core della bozza uguali a quelli del modello principale, senza attesa attiva: con 8 thread di default su 2 core fissati
            // la bozza andava a 4,96 invece di 8,9 tok/s, e con --poll 100 anche sulla bozza si rubano i core a vicenda (Xiaomi, 13/10/2026)
            listOf("--spec-type", "draft-simple", "-md", draft, "-devd", "none", "-ngld", "0", "--spec-draft-n-max", draftMax.toString(), "--spec-draft-n-min", "1",
                "-td", Native.bigCores().toString(), "-tbd", Native.bigCores().toString(), "--poll-draft", "0", "--poll-batch-draft", "0") +
                (Native.bigCoreMask()?.let { listOf("-Cd", it) } ?: emptyList())
        // tutto locale (nessun nodo remoto): flash attention e polling come nelle repliche (+23% sullo Xiaomi con Q4_0, misurato il 13/10/2026)
        val local = if (rpc.isEmpty()) Native.flashAttention() + listOf("--poll", "100") else emptyList()
        // MoE veloce: meno esperti attivi per token (Qwen3-Coder-30B-A3B: 6 su 8 stessa qualità su 22 compiti, 5 ne perde 2, 4 rompe il codice)
        val moe = if (experts > 0) Gguf.read(File(model))?.arch?.let { listOf("--override-kv", "$it.expert_used_count=int:$experts") } ?: emptyList() else emptyList()
        val cmd = listOf(bin.path, "-m", model) + remote + spec + local + moe + listOf(
            "-c", ctxSize.toString(), "-np", "1",
            // generazione limitata dalla banda di memoria: bastano i core big; il prompt scala su tutti i core
            "-t", Native.bigCores().toString(), "-tb", Native.cores().toString(),
        ) + pinBig() + listOf(
            "--host", "0.0.0.0", "--port", Native.API_PORT.toString(),
            "--cors-origins", "*",
            "--metrics",  // tok/s per il pannello di stato (StatusServer.tokS)
        )
        // tiene sveglia la radio Wi-Fi dei worker: senza, ogni token può attendere fino a 0,5 s
        Keepalive.pin(rpc.split(',').filter { it.isNotEmpty() }.map { it.substringBefore(':') })
        val nodes = 1 + if (rpc.isEmpty()) 0 else rpc.split(',').size
        NodeState.apiReady = false
        NodeState.coordinatorInfo = "${File(model).name} su $nodes ${if (nodes == 1) "nodo" else "nodi"}" +
            if (draft.isNotEmpty()) " + bozza ${File(draft).name} (k=$draftMax)" else ""
        coordinator = launch("coord", cmd, mapOf("LLAMA_CACHE" to cacheDir.path)) {
            coordinator = null
            NodeState.coordinatorRunning = false
            NodeState.apiReady = false
            stopIfIdle()
        }
        NodeState.coordinatorRunning = coordinator != null
        if (coordinator != null) pollHealth()
        updateNotification()
    }

    private fun stopCoordinator() {
        Keepalive.unpinAll()
        coordinator?.destroy()
        coordinator = null
        NodeState.coordinatorRunning = false
        NodeState.apiReady = false
        updateNotification()
    }

    /** Aspetta che llama-server abbia distribuito il modello e risponda su /health. */
    private fun pollHealth() = thread(name = "health") {
        val proc = coordinator
        while (proc != null && proc.isAliveCompat() && !NodeState.apiReady) {
            val ok = runCatching {
                val c = URL("http://127.0.0.1:${Native.API_PORT}/health").openConnection() as HttpURLConnection
                c.connectTimeout = 1000; c.readTimeout = 2000
                c.responseCode == 200
            }.getOrDefault(false)
            if (ok) {
                NodeState.apiReady = true
                NodeState.log("coord", "modello pronto: http://${Native.localIp()}:${Native.API_PORT}")
                NodeState.notifyChanged()
                updateNotification()
            } else Thread.sleep(1500)
        }
    }

    // ------------------------------------------------------ repliche e router

    /** Modello intero su questo telefono, servito su :8081 al router. 2 slot: batching di due utenti. */
    private fun startReplica(model: String, ctxSize: Int) {
        if (replica != null) return
        val cmd = listOf(
            Native.llamaServer(this).path, "-m", model,
            // cache totale limitata a 8192 token: con 4 slot sono 2048 a richiesta, abbastanza per il codice e senza strappare RAM ai telefoni da 2-3 GB liberi
            "-c", minOf(ctxSize * Native.replicaSlots(), maxOf(ctxSize, 8192)).toString(), "-np", Native.replicaSlots().toString(),
            "-t", Native.bigCores().toString(), "-tb", Native.cores().toString(),
        ) + pinBig() + Native.flashAttention() + listOf("--poll", "100") + listOf(
            "--host", "0.0.0.0", "--port", Native.REPLICA_PORT.toString(),
            "--cors-origins", "*",
        )
        NodeState.replicaReady = false
        NodeState.replicaState = "caricamento ${File(model).name}"
        replica = launch("replica", cmd, mapOf("LLAMA_CACHE" to cacheDir.path)) {
            replica = null
            NodeState.replicaRunning = false
            NodeState.replicaReady = false
            if (!NodeState.replicaState.startsWith("errore")) NodeState.replicaState = "ferma"
            stopIfIdle()
        }
        NodeState.replicaRunning = replica != null
        replica?.let { p ->
            thread(name = "replica-health") {
                while (p.isAliveCompat() && !NodeState.replicaReady) {
                    if (httpOk("http://127.0.0.1:${Native.REPLICA_PORT}/health")) {
                        NodeState.replicaReady = true
                        NodeState.replicaState = "pronta: ${File(model).name}"
                        NodeState.log("replica", NodeState.replicaState)
                        NodeState.notifyChanged()
                        updateNotification()
                    } else Thread.sleep(1500)
                }
            }
        }
        updateNotification()
    }

    private fun stopReplica() {
        replica?.destroy()
        replica = null
        NodeState.replicaRunning = false
        NodeState.replicaReady = false
        NodeState.replicaState = "ferma"
        updateNotification()
    }

    private fun startRouter(hosts: List<String>, modelName: String) {
        if (router != null || hosts.isEmpty()) return
        val r = Router(hosts)
        if (!r.start()) return
        router = r
        NodeState.router = r
        NodeState.coordinatorRunning = true
        NodeState.coordinatorInfo = "$modelName · ${hosts.size} repliche"
        updateNotification()
    }

    private fun stopRouter() {
        router?.stop()
        router = null
        NodeState.router = null
        NodeState.coordinatorRunning = coordinator != null
        NodeState.apiReady = false
        updateNotification()
    }

    private fun httpOk(url: String) = runCatching {
        val c = URL(url).openConnection() as HttpURLConnection
        c.connectTimeout = 1000; c.readTimeout = 2000
        c.responseCode == 200
    }.getOrDefault(false)

    // ---------------------------------------------------------------- comuni

    private fun launch(tag: String, cmd: List<String>, env: Map<String, String>, onExit: () -> Unit): Process? {
        val bin = File(cmd[0])
        if (!bin.exists()) {
            NodeState.log(tag, "binario mancante: ${bin.name}")
            return null
        }
        NodeState.log(tag, "avvio: ${bin.name} ${cmd.drop(1).joinToString(" ")}")
        return try {
            val pb = ProcessBuilder(cmd).redirectErrorStream(true)
            pb.environment().putAll(env + mapOf("HOME" to filesDir.path, "TMPDIR" to cacheDir.path))
            val p = pb.start()
            acquireLocks()
            thread(name = "$tag-log") {
                p.inputStream.bufferedReader().forEachLine {
                    if (tag == "worker") WorkerStats.onLine(it)
                    NodeState.log(tag, it)
                }
                val code = runCatching { p.waitFor() }.getOrDefault(-1)
                NodeState.log(tag, "processo terminato (codice $code)")
                onExit()
                NodeState.notifyChanged()
            }
            p
        } catch (e: Exception) {
            NodeState.log(tag, "errore avvio: ${e.message}")
            null
        }
    }

    private fun Process.isAliveCompat() = runCatching { exitValue(); false }.getOrDefault(true)

    private fun acquireLocks() {
        if (wakeLock == null) {
            val pm = getSystemService(POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "distai:node").apply { acquire() }
        }
        val wm = applicationContext.getSystemService(WIFI_SERVICE) as WifiManager
        if (wifiLock == null) {
            @Suppress("DEPRECATION")
            val mode = if (Build.VERSION.SDK_INT >= 29) WifiManager.WIFI_MODE_FULL_LOW_LATENCY else WifiManager.WIFI_MODE_FULL_HIGH_PERF
            wifiLock = wm.createWifiLock(mode, "distai:wifi").apply { acquire() }
        }
        if (multicastLock == null) {
            multicastLock = wm.createMulticastLock("distai:mdns").apply { setReferenceCounted(false); acquire() }
        }
    }

    private fun releaseLocks() {
        wakeLock?.takeIf { it.isHeld }?.release(); wakeLock = null
        wifiLock?.takeIf { it.isHeld }?.release(); wifiLock = null
        multicastLock?.takeIf { it.isHeld }?.release(); multicastLock = null
    }

    private fun stopIfIdle() {
        if (worker == null && coordinator == null && replica == null && router == null) {
            releaseLocks()
            stopForegroundCompat()
            stopSelf()
        }
        NodeState.notifyChanged()
    }

    override fun onDestroy() {
        status.stop()
        Keepalive.stop()
        worker?.destroy(); coordinator?.destroy(); replica?.destroy(); router?.stop()
        worker = null; coordinator = null; replica = null; router = null
        NodeState.router = null; NodeState.replicaRunning = false; NodeState.replicaReady = false
        NodeState.workerRunning = false; NodeState.coordinatorRunning = false; NodeState.apiReady = false
        unregisterNsd()
        releaseLocks()
        NodeState.notifyChanged()
        super.onDestroy()
    }

    // --------------------------------------------------------- notifica FGS

    private fun notificationText(): String = buildList {
        if (worker != null) add("Worker attivo su :${Native.RPC_PORT}")
        if (coordinator != null) add(if (NodeState.apiReady) "LLM pronto su :${Native.API_PORT}" else "Caricamento modello…")
        if (replica != null) add(if (NodeState.replicaReady) "Replica pronta" else "Replica in caricamento…")
        router?.let { add("Router: ${it.healthyCount} repliche") }
    }.joinToString(" · ").ifEmpty { "In attesa" }

    private fun buildNotification(): Notification {
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Nodo DistAI", NotificationManager.IMPORTANCE_LOW))
        }
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        @Suppress("DEPRECATION")
        val b = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, CHANNEL) else Notification.Builder(this)
        return b.setContentTitle("DistAI · ${Native.localIp() ?: "offline"}")
            .setContentText(notificationText())
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentIntent(pi)
            .setOngoing(true)
            .build()
    }

    private fun goForeground() {
        val n = buildNotification()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else startForeground(NOTIF_ID, n)
    }

    private fun updateNotification() {
        if (worker == null && coordinator == null && replica == null && router == null) return
        (getSystemService(NOTIFICATION_SERVICE) as NotificationManager).notify(NOTIF_ID, buildNotification())
    }

    private fun stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= 24) stopForeground(STOP_FOREGROUND_REMOVE)
        else @Suppress("DEPRECATION") stopForeground(true)
    }
}
