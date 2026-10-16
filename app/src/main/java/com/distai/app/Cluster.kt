package com.distai.app

import android.content.Context
import android.net.Uri
import org.json.JSONObject
import java.io.File

/**
 * Avvio del modello diviso (split) deciso da questo telefono:
 *  1. mini-benchmark di ogni nodo (RpcBench, ~0,5 s a nodo): banda di calcolo e round-trip;
 *     le voci doppie dello stesso dispositivo (USB e Wi-Fi) diventano un nodo solo sul canale più rapido;
 *  2. coordinatore: quello scelto dall'utente, altrimenti il PC se presente, altrimenti il nodo più veloce;
 *  3. split pesato sulla velocità (SplitPlan);
 *  4. avvio qui oppure delega al coordinatore scelto (/coord/start); in quel caso il router di questo telefono
 *     fa da ponte e l'API resta su http://questo-telefono:8080;
 *  5. modello predittivo provato solo se la stima dice che conviene (Speculative).
 */
class Cluster(private val ctx: Context) {
    companion object {
        const val COORD_AUTO = "auto"
        const val COORD_SELF = "self"
        /** coordinatore delegato in uso (per fermarlo) */
        @Volatile var delegate: Node? = null
        /** ultimo benchmark per endpoint, mostrato nella lista dei nodi */
        val bench = java.util.concurrent.ConcurrentHashMap<String, RpcBench.Result>()

        /**
         * Modello che sta solo sul nodo [node] (un PC con tools/pcnode.py): ne scarico l'intestazione (GET /gguf/<nome>,
         * metadati e descrittori dei tensori, di solito pochi MB) in [dir] e la leggo con la dimensione vera del file.
         * Basta per il piano; i pesi restano sul PC, che fa da coordinatore.
         */
        fun remoteInfo(node: Node, name: String, size: Long, dir: File): Pair<File, Gguf.Info>? {
            val f = File(dir, name)
            for (bytes in listOf(32L shl 20, 128L shl 20)) {
                runCatching {
                    val url = java.net.URL("http://${node.host}:${node.statusPort}/gguf/${Uri.encode(name)}?bytes=$bytes")
                    val c = url.openConnection() as java.net.HttpURLConnection
                    c.connectTimeout = 3000; c.readTimeout = 60_000
                    c.inputStream.use { inp -> f.outputStream().use { inp.copyTo(it) } }
                }
                Gguf.read(f, size)?.let { return f to it }
            }
            f.delete()
            return null
        }
    }

    data class Start(
        val model: File, val info: Gguf.Info, val ctxSize: Int,
        val coord: Node?, val plan: SplitPlan.Ok, val coordGbps: Double,
        /** endpoint dei worker come li vede il coordinatore */
        val rpc: String,
    )

    private fun log(msg: String) = NodeState.log("cluster", msg)

    /** Banda del worker locale (questo telefono) e dei nodi; aggiorna [bench]. */
    fun measure(nodes: List<Node>): List<Node> = nodes.map { n ->
        val r = RpcBench.run(n.host, n.port)
        if (r != null) {
            bench[n.endpoint] = r
            log("${n.name} (${n.endpoint}): ${"%.1f".format(r.gbps)} GB/s · rtt ${"%.1f".format(r.rttMs)} ms")
            n.copy(gbps = r.gbps, rttMs = r.rttMs)
        } else n
    }

    /**
     * Voci dello stesso dispositivo (stesso nome e RAM da /status) → una sola, sul canale col round-trip minore.
     * Con un adb aggiornato adb e tethering hanno banda simile (Samsung, 13/10/2026: adb 237/35 MB/s, tethering 30/40),
     * quindi decide la latenza, come in tools/pcnode.py.
     */
    fun merge(nodes: List<Node>): List<Node> = nodes.groupBy { it.devId.ifEmpty { it.endpoint } }.values.map { same ->
        val best = same.filter { it.gbps > 0 }.minByOrNull { it.rttMs } ?: same.first()
        if (same.size > 1) log("${best.name}: ${same.size} canali (${same.joinToString { it.endpoint }}), uso ${best.endpoint}")
        best.copy(gbps = same.maxOf { it.gbps })
    }

    /**
     * Prepara il piano. [choice]: [COORD_AUTO], [COORD_SELF] o il devId del nodo scelto a mano.
     * [self] è il worker di questo telefono (127.0.0.1:50052) con la sua RAM libera.
     */
    fun plan(model: File, ctxSize: Int, self: Node, others: List<Node>, choice: String,
             remoteModel: Pair<Gguf.Info, String>? = null): Start? {
        // remoteModel: intestazione già letta di un modello che sta solo sul nodo con quel devId, che deve fare da coordinatore
        val info = remoteModel?.first ?: Gguf.read(model) ?: run { log("file GGUF non leggibile"); return null }
        val myIp = Native.localIp()
        val all = measure(listOf(self) + others)
        val me = all.first()
        val remote = merge(all.drop(1).filter { it.devId != me.devId || it.devId.isEmpty() })

        val candidates = remote.filter { it.canCoord && it.gbps > 0 }
        if (choice != COORD_SELF && choice != COORD_AUTO && candidates.none { it.devId == choice })
            log("coordinatore scelto non disponibile: uso questo telefono")
        // in automatico provo ogni coordinatore possibile (questo telefono, i PC, gli altri telefoni) e tengo il piano
        // con più tok/s stimati: un PC senza RAM libera o lento non viene più preferito solo perché è un PC
        val options: List<Node?> = when {
            remoteModel != null -> candidates.filter { it.devId == remoteModel.second }.ifEmpty {
                log("il nodo che ha ${model.name} non risponde come coordinatore"); return null
            }
            choice == COORD_SELF -> listOf(null)
            choice == COORD_AUTO -> listOf(null) + candidates
            else -> listOf(candidates.firstOrNull { it.devId == choice })
        }
        val plans = options.mapNotNull { c ->
            planWith(info, ctxSize, c, me, remote, myIp, quiet = options.size > 1)?.let { (p, w) -> Triple(c, p, w) }
        }
        val ok = plans.filter { it.second is SplitPlan.Ok }
        if (ok.isEmpty()) { plans.firstOrNull()?.second?.let { log((it as SplitPlan.TooBig).describe()) }; return null }
        val (coord, best, _) = ok.maxBy { (it.second as SplitPlan.Ok).estTokS }
        if (ok.size > 1) log("coordinatore: " + ok.joinToString(" · ") { (c, p, _) ->
            "${c?.name ?: "questo telefono"} ${"%.1f".format((p as SplitPlan.Ok).estTokS)} tok/s" })
        val plan = best as SplitPlan.Ok
        log(plan.describe(coord?.name ?: "questo telefono"))
        NodeState.planInfo = plan.describe(coord?.name ?: "questo telefono")
        val coordGbps = coord?.gbps ?: me.gbps
        return Start(model, info, ctxSize, coord, plan, coordGbps, plan.remote.joinToString(",") { it.endpoint })
    }

    /**
     * Piano con il coordinatore [coord] (null = questo telefono): i suoi worker e la ripartizione dei layer.
     * null se il coordinatore delegato non è raggiungibile in rete da qui.
     */
    private fun planWith(info: Gguf.Info, ctxSize: Int, coord: Node?, me: Node, remote: List<Node>, myIp: String?,
                         quiet: Boolean): Pair<SplitPlan, List<Node>>? {
        // un coordinatore delegato raggiunge gli altri via Wi-Fi: i canali USB visti da qui non valgono per lui
        // (il PC sceglie da solo tra cavo e Wi-Fi per i telefoni collegati, vedi tools/pcnode.py)
        val workers: List<Node> = if (coord == null) remote else {
            if (myIp == null) { log("nessuna rete: non posso delegare il coordinatore"); return null }
            val mine = me.copy(host = myIp, port = Native.RPC_PORT)
            (listOf(mine) + remote.filter { it.devId != coord.devId }).mapNotNull { n ->
                // nodo della rete visto via cavo tramite il PC: al coordinatore serve il suo indirizzo vero
                if (n.host == "127.0.0.1" && n.lanEndpoint.isNotEmpty())
                    n.copy(host = n.lanEndpoint.substringBeforeLast(':'), port = n.lanEndpoint.substringAfterLast(':').toInt())
                else if (n.host != "127.0.0.1") n else {
                    if (!quiet) log("${n.name} è collegato solo via USB a questo telefono: escluso dal coordinatore ${coord.name}")
                    null
                }
            }
        }
        val coordFree = coord?.freeMb ?: Native.memInfo(ctx).second
        // il coordinatore è questo telefono (null) o un nodo remoto: se è un telefono vale il margine di Android
        return SplitPlan.make(info, ctxSize, coordFree, workers, coord?.gbps ?: me.gbps,
            localAndroid = coord?.android ?: true) to workers
    }

    /** Avvia il coordinatore secondo il piano, con la bozza [draft] se data. */
    fun launch(s: Start, draft: File? = null, draftMax: Int = 4, experts: Int = 0) {
        val coord = s.coord
        if (coord == null) {
            delegate = null
            NodeService.send(ctx, NodeService.ACTION_COORD_START) {
                putExtra(NodeService.EXTRA_MODEL, s.model.path)
                putExtra(NodeService.EXTRA_RPC, s.rpc)
                putExtra(NodeService.EXTRA_NGL, s.plan.remoteLayers)
                putExtra(NodeService.EXTRA_TS, s.plan.remoteSplit.joinToString(","))
                putExtra(NodeService.EXTRA_CTX, s.ctxSize)
                draft?.let { putExtra(NodeService.EXTRA_DRAFT, it.path); putExtra(NodeService.EXTRA_DRAFT_MAX, draftMax) }
                putExtra(NodeService.EXTRA_EXPERTS, experts)
            }
            return
        }
        if (!startRemote(s, coord, draft, draftMax, experts)) return
        delegate = coord
        // ponte: l'API OpenAI resta su questo telefono (:8080), il router la gira al coordinatore
        NodeService.send(ctx, NodeService.ACTION_ROUTER_START) {
            putStringArrayListExtra(NodeService.EXTRA_HOSTS, arrayListOf("${coord.host}:${coord.apiPort}"))
            putExtra(NodeService.EXTRA_MODEL, "${s.model.name} · coordinatore ${coord.name}")
        }
    }

    /** /coord/start sul nodo scelto; il coordinatore scarica da qui i modelli che non ha. */
    private fun startRemote(s: Start, coord: Node, draft: File?, draftMax: Int, experts: Int = 0): Boolean {
        val src = { f: File -> "http://${Native.localIp()}:${Native.STATUS_PORT}/files/${Uri.encode(f.name)}" }
        val body = JSONObject().put("model", s.model.name).put("source", src(s.model))
            .put("rpc", s.rpc).put("ngl", s.plan.remoteLayers).put("ts", s.plan.remoteSplit.joinToString(","))
            .put("ctx", s.ctxSize)
        draft?.let { body.put("draft", it.name).put("draftSource", src(it)).put("draftMax", draftMax) }
        if (experts > 0) body.put("experts", experts)
        val ok = StatusServer.post(coord.host, "/coord/start", body, port = coord.statusPort)
        log(if (ok) "coordinatore delegato a ${coord.name} (${coord.host}:${coord.apiPort})" else "${coord.name} non accetta il ruolo di coordinatore")
        return ok
    }

    fun stop() {
        NodeService.send(ctx, NodeService.ACTION_COORD_STOP)
        NodeService.send(ctx, NodeService.ACTION_ROUTER_STOP)
        delegate?.let { d -> StatusServer.post(d.host, "/coord/stop", port = d.statusPort) }
        delegate = null
    }

    /**
     * Dopo l'avvio: se c'è una bozza compatibile e la stima dice che conviene, riavvia con la bozza
     * e la tiene solo se misura davvero più tok/s. Da chiamare fuori dal main thread.
     */
    fun tuneSpeculative(s: Start) {
        val api = "http://127.0.0.1:${Native.API_PORT}"
        if (!Speculative.waitReady(api, 30 * 60_000L)) return
        val (draft, dInfo) = Speculative.findDraft(ModelStore.list(ctx), s.model, s.info)
            ?: return NodeState.log("bozza", "nessun modello piccolo compatibile (stesso tokenizer) tra quelli scaricati: bozza non usata")
        // la bozza deve entrare nella RAM del coordinatore oltre alla sua parte del modello
        val freeAfter = s.plan.budgetMb[0] - s.plan.needMb[0]
        val draftMb = (draft.length() + dInfo.kvBytesPerLayer(s.ctxSize) * dInfo.layers) / 1048576
        if (draftMb > freeAfter) return NodeState.log("bozza", "${draft.name} non entra nel coordinatore ($draftMb MB, liberi $freeAfter)")
        val calib = Speculative.calibrate(api) ?: return
        val base = 1000 / calib.t1Ms
        val choice = Speculative.choose(draft, s.coordGbps, calib, Speculative.lastP(ctx))
        if (choice.gain < Speculative.MIN_GAIN) {
            return NodeState.log("bozza", "non conviene (guadagno stimato ${"%.2f".format(choice.gain)} < ${Speculative.MIN_GAIN}): resta spenta")
        }
        NodeState.log("bozza", "provo ${draft.name} con k=${choice.k} (base ${"%.2f".format(base)} tok/s)")
        restart(s, draft, choice.k)
        // il router si accorge del riavvio dopo qualche secondo: prima aspetto che l'API sparisca
        Speculative.waitDown(api, 10_000)
        if (!Speculative.waitReady(api, 10 * 60_000L)) return
        val m = Speculative.measure(api)
        m?.second?.let { Speculative.saveP(ctx, it) }
        if (m == null || m.first < base * 1.05) {
            NodeState.log("bozza", "misurato ${"%.2f".format(m?.first ?: 0.0)} tok/s (accettazione ${m?.second?.let { "%.0f%%".format(it * 100) } ?: "?"}): " +
                "non più veloce, torno senza bozza")
            restart(s, null, 0)
        } else NodeState.log("bozza", "attiva: ${"%.2f".format(m.first)} tok/s contro ${"%.2f".format(base)} " +
            "(accettazione ${m.second?.let { "%.0f%%".format(it * 100) } ?: "?"})")
    }

    private fun restart(s: Start, draft: File?, k: Int) {
        val coord = s.coord
        if (coord == null) {
            NodeService.send(ctx, NodeService.ACTION_COORD_STOP)
            Thread.sleep(1500) // i worker chiudono la connessione e tornano liberi
            launch(s, draft, k)
        } else {
            // il router resta acceso: torna pronto quando il coordinatore riavviato risponde
            StatusServer.post(coord.host, "/coord/stop", port = coord.statusPort)
            Thread.sleep(1500)
            startRemote(s, coord, draft, k)
        }
    }
}
