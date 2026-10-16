package com.distai.app

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Handler
import android.os.Looper
import java.net.InetSocketAddress
import java.net.Socket

/** Un telefono che espone ggml-rpc-server sulla LAN. */
data class Node(
    val host: String,
    val port: Int = Native.RPC_PORT,
    val name: String = host,
    val ramMb: Long = 0,
    val freeMb: Long = 0,
    val cpu: String = "",
    val manual: Boolean = false,
    /** "coordinatore", "replica", "worker" o vuoto, da /status */
    val role: String = "",
    /** nome del dispositivo da /status: due voci con lo stesso id sono due canali (USB e Wi-Fi) dello stesso nodo */
    val devId: String = "",
    /** porta dell'API OpenAI quando il nodo fa da coordinatore (il PC usa la 8090: la 8080 è occupata) */
    val apiPortRemote: Int = Native.API_PORT,
    /** il nodo può fare da coordinatore (telefoni con l'app, PC con tools/pcnode.py) */
    val canCoord: Boolean = false,
    val x86: Boolean = false,
    /** risultati di RpcBench: round-trip del canale e banda di calcolo; 0 = non misurato */
    val rttMs: Double = 0.0,
    val gbps: Double = 0.0,
    /** da /status: coordinatori collegati al suo worker, MB occupati dai pesi, CPU % del worker */
    val clients: Int = 0,
    val workMb: Long = 0,
    val cpuPct: Int = 0,
    /** da /status, se fa da coordinatore: modello pronto, descrizione e tok/s medi */
    val apiReady: Boolean = false,
    val coordState: String = "",
    val tokS: Double = 0.0,
    /** bench che il nodo ha fatto su se stesso all'avvio (SelfBench, pcnode.py): banda GB/s e GFLOPS; 0 = assente */
    val benchGbps: Double = 0.0,
    val benchGflops: Double = 0.0,
    /** nodo della rete raggiunto via cavo tramite il PC (127.0.0.1:porta): il suo indirizzo vero sulla Wi-Fi */
    val lanEndpoint: String = "",
    /** stesso PC raggiunto sulla rete del cavo (tethering USB o Ethernet) invece che con adb reverse */
    val cable: Boolean = false,
    /** modelli GGUF del nodo (nome -> byte, 0 se non noto), da /status: un PC coordinatore li usa senza copiarli */
    val models: Map<String, Long> = emptyMap(),
) {
    val endpoint get() = "$host:$port"
    /** /status sta su 50053; un nodo su una porta RPC diversa (es. il PC via USB su 50072) lo espone sulla porta successiva */
    val statusPort get() = if (port == Native.RPC_PORT) Native.STATUS_PORT else port + 1
    /** l'API del coordinatore; via USB (adb reverse) sta sulla porta RPC + 2 */
    val apiPort get() = if (port == Native.RPC_PORT) apiPortRemote else port + 2
    /** telefono Android con l'app (varianti v8/v82/v86); PC, Raspberry e nodi GPU di pcnode.py no */
    val android get() = cpu.substringAfter('/').startsWith("v8")
    /** canale via cavo: 127.0.0.1 su una porta diversa da quella del worker locale */
    val usb get() = host == "127.0.0.1" && port != Native.RPC_PORT
}

/** Scopre i worker via mDNS (NSD). Le risoluzioni sono serializzate: Android ne regge una alla volta. */
class Discovery(ctx: Context, private val onChange: (List<Node>) -> Unit) {
    private val nsd = ctx.getSystemService(Context.NSD_SERVICE) as NsdManager
    private val main = Handler(Looper.getMainLooper())
    private val nodes = linkedMapOf<String, Node>()
    private val pending = ArrayDeque<NsdServiceInfo>()
    private var resolving = false
    private var listener: NsdManager.DiscoveryListener? = null

    fun start() {
        if (listener != null) return
        val l = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(type: String) = NodeState.log("nsd", "ricerca nodi avviata")
            override fun onDiscoveryStopped(type: String) {}
            override fun onStartDiscoveryFailed(type: String, err: Int) = NodeState.log("nsd", "ricerca fallita ($err)")
            override fun onStopDiscoveryFailed(type: String, err: Int) {}
            override fun onServiceFound(si: NsdServiceInfo) = main.post { pending.add(si); resolveNext() }.let {}
            override fun onServiceLost(si: NsdServiceInfo) = main.post {
                nodes.entries.removeAll { !it.value.manual && it.value.name == si.serviceName }
                publish()
            }.let {}
        }
        listener = l
        nsd.discoverServices(NodeService.NSD_TYPE, NsdManager.PROTOCOL_DNS_SD, l)
    }

    fun stop() {
        listener?.let { runCatching { nsd.stopServiceDiscovery(it) } }
        listener = null
    }

    /** Rilegge /status di tutti i nodi (RAM libera, ruolo) fuori dal main thread e ripubblica la lista. */
    fun refreshAll() = Thread {
        val fresh = nodes.values.toList().map { refresh(it) }
        // ricerca via ADB: se il telefono è sul cavo di un PC con tools/pcnode.py, adb reverse porta il PC su
        // 127.0.0.1:50072 (status sulla successiva). Lo aggiungo da solo: serve quando la Wi-Fi è un'altra rete.
        val pcStatus = StatusServer.fetch("127.0.0.1", 700, USB_PC_PORT + 1)
        val usbPc = pcStatus?.let { j -> refresh(Node("127.0.0.1", USB_PC_PORT, j.optString("dev", "PC") + " (USB)")) }
        // tethering USB (o Ethernet) acceso: lo stesso PC è raggiungibile anche sulla rete del cavo, che non dipende da adb;
        // Cluster.merge sceglie tra i due canali in base al round-trip.
        val wired = Native.wiredIps().map { it.substringBeforeLast('.') }
        val cablePc = pcStatus?.optJSONArray("lanIps")?.let { a -> (0 until a.length()).map { a.getString(it) } }.orEmpty()
            .filter { it.substringBeforeLast('.') in wired }
            .firstNotNullOfOrNull { ip ->
                StatusServer.fetch(ip, 700, Native.STATUS_PORT)?.let { j ->
                    refresh(Node(ip, Native.RPC_PORT, j.optString("dev", "PC") + " (cavo)", cable = true))
                }
            }
        // telefono isolato (altra Wi-Fi): il PC porta qui anche gli altri nodi della rete (usbPeers, adb reverse)
        val peers = pcStatus?.optJSONArray("usbPeers")?.let { a -> (0 until a.length()).map { a.getJSONObject(it) } }.orEmpty()
            .mapNotNull { p ->
                val n = Node("127.0.0.1", p.optInt("port"), p.optString("dev") + " (via PC)", lanEndpoint = p.optString("lan"))
                StatusServer.fetch(n.host, 700, n.statusPort)?.let { refresh(n) }
            }
        main.post {
            fresh.forEach { n -> if (nodes.containsKey(n.endpoint)) nodes[n.endpoint] = n }
            val key = "127.0.0.1:$USB_PC_PORT"
            if (usbPc != null) nodes[key] = nodes[key]?.takeIf { it.manual }?.let { usbPc.copy(manual = true) } ?: usbPc
            else if (nodes[key]?.manual == false) nodes.remove(key)
            nodes.entries.removeAll { it.value.cable && it.key != cablePc?.endpoint }
            cablePc?.let { nodes[it.endpoint] = it }
            nodes.entries.removeAll { it.value.lanEndpoint.isNotEmpty() && peers.none { p -> p.endpoint == it.key } }
            peers.forEach { nodes[it.endpoint] = it }
            publish()
        }
    }.start()

    fun addManual(host: String, port: Int = Native.RPC_PORT) {
        nodes["$host:$port"] = Node(host, port, "$host (manuale)", manual = true)
        publish()
    }

    @Suppress("DEPRECATION")
    private fun resolveNext() {
        if (resolving) return
        val si = pending.removeFirstOrNull() ?: return
        resolving = true
        nsd.resolveService(si, object : NsdManager.ResolveListener {
            override fun onResolveFailed(si: NsdServiceInfo, err: Int) = main.post { resolving = false; resolveNext() }.let {}
            override fun onServiceResolved(si: NsdServiceInfo) = main.post {
                resolving = false
                val host = si.host?.hostAddress
                if (host != null && !host.contains(':')) {
                    fun attr(k: String) = si.attributes[k]?.let { String(it) }
                    nodes["$host:${si.port}"] = Node(
                        host, si.port, attr("dev") ?: si.serviceName,
                        attr("ram")?.toLongOrNull() ?: 0, attr("free")?.toLongOrNull() ?: 0, attr("cpu") ?: "",
                    )
                    publish()
                }
                resolveNext()
            }.let {}
        })
    }

    private fun publish() = onChange(nodes.values.toList())

    companion object {
        /** porta del PC via cavo: tools/pcnode.py fa `adb reverse tcp:50072 tcp:50052` (status su 50073) */
        const val USB_PC_PORT = 50072

        /** Aggiorna RAM libera e CPU del nodo con lo stato in tempo reale, se il nodo lo espone. */
        fun refresh(n: Node): Node = StatusServer.fetch(n.host, port = n.statusPort)?.let { j ->
            n.copy(
                name = if (n.manual) j.optString("dev", n.name) else n.name,
                ramMb = j.optLong("ram", n.ramMb), freeMb = j.optLong("free", n.freeMb),
                cpu = "${j.optInt("cores")}/${j.optString("variant")}",
                role = when {
                    j.optBoolean("coordinator") -> "coordinatore"
                    j.optBoolean("replica") -> "replica"
                    j.optBoolean("worker") -> "worker"
                    else -> ""
                },
                devId = "${j.optString("dev")}/${j.optLong("ram")}",
                apiPortRemote = j.optInt("apiPort", Native.API_PORT),
                canCoord = j.optBoolean("canCoord", false),
                x86 = j.optString("variant") == "x86",
                clients = j.optInt("workerClients"), workMb = j.optLong("workerMb"), cpuPct = j.optInt("workerCpu"),
                apiReady = j.optBoolean("apiReady"), coordState = j.optString("coordState"), tokS = j.optDouble("tokS", 0.0),
                benchGbps = j.optJSONObject("bench")?.optDouble("gbps", 0.0) ?: 0.0,
                benchGflops = j.optJSONObject("bench")?.optDouble("gflops", 0.0) ?: 0.0,
                models = j.optJSONArray("models")?.let { a ->
                    val sizes = j.optJSONObject("modelSizes")
                    (0 until a.length()).associate { a.getString(it) to (sizes?.optLong(a.getString(it)) ?: 0L) }
                } ?: n.models,
            )
        } ?: n

        /** Verifica che la porta RPC del nodo sia raggiungibile (da chiamare fuori dal main thread). */
        fun reachable(n: Node, timeoutMs: Int = 1500): Boolean = runCatching {
            Socket().use { it.connect(InetSocketAddress(n.host, n.port), timeoutMs) }
            true
        }.getOrDefault(false)
    }
}
