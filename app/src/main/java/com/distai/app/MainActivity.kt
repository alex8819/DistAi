package com.distai.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.RadioButton
import android.widget.Spinner
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import org.json.JSONObject
import java.io.File
import kotlin.concurrent.thread

@SuppressLint("UseSwitchCompatOrMaterialCode", "SetTextI18n")
class MainActivity : Activity() {

    private lateinit var info: TextView
    private lateinit var statusCard: View
    private lateinit var statusText: TextView
    private lateinit var pulse: TextView
    private val pulseAnim by lazy {
        android.animation.ObjectAnimator.ofFloat(pulse, "alpha", 1f, 0.25f).apply {
            duration = 700; repeatCount = android.animation.ValueAnimator.INFINITE
            repeatMode = android.animation.ValueAnimator.REVERSE
        }
    }
    /** tok/s del coordinatore di questo telefono, letti da /metrics fuori dal main thread */
    @Volatile private var localTokS = 0.0
    private lateinit var workerSwitch: Switch
    private lateinit var nodesTitle: TextView
    private lateinit var nodesBox: LinearLayout
    private lateinit var modelSpinner: Spinner
    private lateinit var customUrl: EditText
    private lateinit var progress: ProgressBar
    private lateinit var downloadBtn: Button
    private lateinit var ctxField: EditText
    private lateinit var coordBtn: Button
    private lateinit var apiStatus: TextView
    private lateinit var chatBtn: Button
    private lateinit var logView: TextView

    private lateinit var discovery: Discovery
    private var nodes = listOf<Node>()
    /** endpoint dei nodi deselezionati dall'utente (di default tutti selezionati) */
    private val excluded = mutableSetOf<String>()
    /** voci dello spinner: file locali o preset da scaricare */
    private var modelItems = listOf<Any>()
    @Volatile private var downloading = false
    /** worker remoti su cui è stata avviata una replica (per fermarle) */
    private var replicaHosts = listOf<String>()
    private lateinit var modeReplica: RadioButton
    private lateinit var coordChoice: Spinner
    /** voci del menu coordinatore: COORD_AUTO, COORD_SELF o devId di un nodo */
    private var coordKeys = listOf<String>()

    private val prefs by lazy { getSharedPreferences("distai", MODE_PRIVATE) }
    private val stateListener: () -> Unit = { render() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        info = findViewById(R.id.info)
        statusCard = findViewById(R.id.statusCard)
        statusText = findViewById(R.id.statusText)
        pulse = findViewById(R.id.pulse)
        workerSwitch = findViewById(R.id.workerSwitch)
        nodesTitle = findViewById(R.id.nodesTitle)
        nodesBox = findViewById(R.id.nodes)
        findViewById<TextView>(R.id.nodesLegend).text = android.text.SpannableStringBuilder().also { sb ->
            icons(listOf(R.drawable.ic_home), "coordinatore     ", sb)
            icons(listOf(R.drawable.ic_wifi), "Wi-Fi     ", sb)
            icons(listOf(R.drawable.ic_usb), "USB     ", sb)
            icons(listOf(R.drawable.ic_wifi, R.drawable.ic_usb), "entrambi", sb)
        }
        modelSpinner = findViewById(R.id.model)
        customUrl = findViewById(R.id.customUrl)
        progress = findViewById(R.id.progress)
        downloadBtn = findViewById(R.id.download)
        ctxField = findViewById(R.id.ctx)
        coordBtn = findViewById(R.id.coordBtn)
        apiStatus = findViewById(R.id.apiStatus)
        chatBtn = findViewById(R.id.chatBtn)
        logView = findViewById(R.id.log)
        modeReplica = findViewById(R.id.modeReplica)
        if (prefs.getBoolean("replica", false)) modeReplica.isChecked = true
        modeReplica.setOnCheckedChangeListener { _, v -> prefs.edit().putBoolean("replica", v).apply() }
        coordChoice = findViewById(R.id.coordChoice)
        coordChoice.onItemSelectedListener = object : android.widget.AdapterView.OnItemSelectedListener {
            override fun onItemSelected(p: android.widget.AdapterView<*>?, v: View?, pos: Int, id: Long) {
                coordKeys.getOrNull(pos)?.let { prefs.edit().putString("coord", it).apply() }
            }
            override fun onNothingSelected(p: android.widget.AdapterView<*>?) {}
        }
        renderCoordChoice()
        findViewById<Button>(R.id.benchBtn).setOnClickListener { onBench() }

        if (Build.VERSION.SDK_INT >= 33) requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)

        workerSwitch.setOnClickListener {
            NodeService.send(this, if (workerSwitch.isChecked) NodeService.ACTION_WORKER_START else NodeService.ACTION_WORKER_STOP)
        }
        findViewById<CheckBox>(R.id.moeFast).apply {
            isChecked = prefs.getBoolean("moefast", false)
            setOnCheckedChangeListener { _, v -> prefs.edit().putBoolean("moefast", v).apply() }
        }
        findViewById<CheckBox>(R.id.autoStart).apply {
            isChecked = prefs.getBoolean("autostart", true)
            setOnCheckedChangeListener { _, v -> prefs.edit().putBoolean("autostart", v).apply() }
        }
        findViewById<CheckBox>(R.id.keepScreen).apply {
            isChecked = prefs.getBoolean("keepscreen", true)
            applyKeepScreen(isChecked)
            setOnCheckedChangeListener { _, v -> prefs.edit().putBoolean("keepscreen", v).apply(); applyKeepScreen(v) }
        }
        findViewById<Button>(R.id.battery).setOnClickListener { askBatteryExemption() }
        findViewById<Button>(R.id.freeRam).setOnClickListener { onFreeRam(it as Button) }
        findViewById<Button>(R.id.addIp).setOnClickListener {
            val f = findViewById<EditText>(R.id.manualIp)
            val txt = f.text.toString().trim()
            if (txt.isNotEmpty()) {
                val host = txt.substringBefore(':')
                val port = txt.substringAfter(':', "").toIntOrNull() ?: Native.RPC_PORT
                discovery.addManual(host, port)
                // salvati: prima si perdevano a ogni riavvio dell'app
                prefs.edit().putStringSet("manual", (prefs.getStringSet("manual", emptySet()).orEmpty() + "$host:$port")).apply()
                f.text.clear()
                f.clearFocus() // con il focus il campo riportava la pagina su di sé a ogni aggiornamento
            }
        }
        downloadBtn.setOnClickListener { onDownload() }
        coordBtn.setOnClickListener { onCoordinator() }
        chatBtn.setOnClickListener { startActivity(Intent(this, ChatActivity::class.java)) }
        findViewById<Button>(R.id.clearLog).setOnClickListener { NodeState.clearLog() }
        ctxField.setText(prefs.getInt("ctx", 4096).toString())

        discovery = Discovery(this) { nodes = it; renderNodes(); refreshModels() }
        prefs.getStringSet("manual", emptySet()).orEmpty().forEach {
            discovery.addManual(it.substringBefore(':'), it.substringAfter(':').toIntOrNull() ?: Native.RPC_PORT)
        }
        refreshModels()

        // in negozio: si installa l'APK, si apre l'app e il telefono è già un worker
        if (savedInstanceState == null && prefs.getBoolean("autostart", true) && !NodeState.workerRunning) {
            NodeService.send(this, NodeService.ACTION_WORKER_START)
        }
    }

    /** ogni 5 s rilegge /status dei nodi: RAM libera e ruolo (coordinatore, replica, worker) restano aggiornati */
    private val statusTick = object : Runnable {
        override fun run() {
            discovery.refreshAll()
            thread(name = "stato") {
                WorkerStats.sample()
                localTokS = if (NodeState.apiReady) StatusServer.tokS("127.0.0.1", Native.API_PORT) ?: 0.0 else 0.0
                runOnUiThread { render(); renderNodes() }
            }
            window.decorView.postDelayed(this, 5000)
        }
    }

    override fun onStart() {
        super.onStart()
        NodeState.addListener(stateListener)
        discovery.start()
        window.decorView.post(statusTick)
        render()
    }

    override fun onStop() {
        pulseAnim.cancel()
        window.decorView.removeCallbacks(statusTick)
        NodeState.removeListener(stateListener)
        discovery.stop()
        super.onStop()
    }

    // ---------------------------------------------------------------- render

    private fun render() {
        renderStatus()
        info.text = Native.summary(this) + SelfBench.describe(SelfBench.result).let { if (it.isEmpty()) "" else "\n$it" } +
            if (SelfBench.running) "\n⏳ bench di questo dispositivo in corso…" else ""
        workerSwitch.isChecked = NodeState.workerRunning
        coordBtn.text = if (NodeState.coordinatorRunning) "Ferma LLM distribuito" else "Avvia LLM distribuito"
        coordBtn.backgroundTintList = android.content.res.ColorStateList.valueOf(
            if (NodeState.coordinatorRunning) 0xFFC62828.toInt() else 0xFF2E7D32.toInt())
        chatBtn.isEnabled = NodeState.apiReady
        val routerInfo = NodeState.router?.status()?.optJSONArray("replicas")?.let { a ->
            val list = (0 until a.length()).map { a.getJSONObject(it) }
            "\nRepliche pronte ${list.count { it.optBoolean("healthy") }}/${list.size}: " +
                list.joinToString(" · ") { "${it.optString("host")} ${if (it.optBoolean("healthy")) "✓" else "…"} (${it.optInt("served")} risp.)" }
        }.orEmpty()
        apiStatus.text = when {
            NodeState.apiReady ->
                "✅ ${NodeState.coordinatorInfo}\nAPI OpenAI: http://${Native.localIp()}:${Native.API_PORT}/v1 " +
                    "(usabile anche da PC sulla stessa rete)" + routerInfo
            NodeState.coordinatorRunning -> "⏳ Distribuzione del modello ai nodi… (${NodeState.coordinatorInfo})$routerInfo"
            else -> ""
        } + if (NodeState.planInfo.isNotEmpty() && (NodeState.coordinatorRunning || NodeState.router != null))
            "\n\n📋 ${NodeState.planInfo}" else ""
        logView.text = NodeState.logText().lines().takeLast(40).joinToString("\n")
    }

    /**
     * Card in alto: dispositivi collegati, stato del modello (caricamento / operativo con tok/s) e cosa sta facendo
     * questo telefono. Verde = modello operativo, giallo = caricamento, il pallino pulsa quando qualcosa calcola.
     */
    private fun renderStatus() {
        val myIp = Native.localIp()
        val others = nodeGroups().filter { g -> g.none { isSelf(it, myIp) } }
        val names = others.joinToString(", ") { g -> g.first().name.removePrefix("PC ") }
        val devices = "📱 Dispositivi collegati: ${others.size + 1}" + if (names.isNotEmpty()) " (questo + $names)" else " (solo questo)"
        val remoteCoord = nodes.firstOrNull { it.role == "coordinatore" && !isSelf(it, myIp) }
        val tok = { t: Double -> if (t > 0) " · ${"%.1f".format(t)} tok/s" else "" }
        val (model, color) = when {
            NodeState.apiReady -> "🧠 Modello operativo ✓ · ${NodeState.coordinatorInfo}${tok(localTokS)}" to 0xFFE8F5E9.toInt()
            NodeState.coordinatorRunning -> "⏳ Caricamento del modello sui nodi… · ${NodeState.coordinatorInfo}" to 0xFFFFF8E1.toInt()
            remoteCoord?.apiReady == true ->
                "🧠 Modello operativo ✓ su ${remoteCoord.name}${tok(remoteCoord.tokS)}" to 0xFFE8F5E9.toInt()
            remoteCoord != null -> "⏳ ${remoteCoord.name} sta caricando il modello" to 0xFFFFF8E1.toInt()
            else -> "⚪ Nessun modello avviato" to 0xFFFFFFFF.toInt()
        }
        val busy = WorkerStats.clients > 0
        val worker = when {
            !NodeState.workerRunning -> "⏸ Worker di questo telefono spento"
            busy -> "⚙️ Questo telefono lavora per il cluster · pesi ${gb(WorkerStats.rssMb)} · CPU ${WorkerStats.cpuPct}%"
            else -> "💤 Worker pronto, in attesa di un coordinatore"
        }
        // telefoni che il PC espone col ponte USB (tools/pcnode.py): funzionano, ma ogni token passa anche dal PC
        val viaPc = others.map { it.first().name }.filter { it.endsWith("(USB del PC)") }
        val hint = if (viaPc.isEmpty()) null else
            "🔌 ${viaPc.joinToString { it.removeSuffix(" (USB del PC)") }} collegato tramite il PC (USB): " +
                "per meno latenza attiva il Tethering USB o mettilo sulla stessa rete"
        // questo telefono vede gli altri solo via cavo: tipicamente è su un'altra rete Wi-Fi
        val onlyUsb = if (others.isNotEmpty() && others.all { g -> g.all { it.usb } })
            "🔌 Collegato al cluster solo via USB tramite il PC: va bene se è l'unico telefono; " +
                "per lavorare insieme agli altri telefoni mettilo sulla loro stessa rete" else null
        // misurato col Samsung (13/10/2026): cavo 30-237 MB/s (tethering, adb aggiornato), Wi-Fi 2 MB/s: la banda decide prompt e pesi
        val wired = if (others.isNotEmpty() && !Native.wiredLink() && others.none { g -> g.any { it.usb || it.cable } })
            "💡 Consigliato un cavo: USB verso il PC con DistAI, Tethering USB o un adattatore di rete USB-C: " +
                "da 15 a 100 volte la banda del Wi-Fi" else null
        statusText.text = listOfNotNull(devices, model, worker, hint, onlyUsb, wired).joinToString("\n")
        statusCard.backgroundTintList = android.content.res.ColorStateList.valueOf(color)
        val active = NodeState.coordinatorRunning || (busy && WorkerStats.cpuPct >= 10)
        pulse.setTextColor(if (active || color != 0xFFFFFFFF.toInt()) 0xFF2E7D32.toInt() else 0xFF9E9E9E.toInt())
        if (active && !pulseAnim.isRunning) pulseAnim.start()
        if (!active && pulseAnim.isRunning) { pulseAnim.cancel(); pulse.alpha = 1f }
    }

    private fun gb(mb: Long) = if (mb >= 1024) "%.1f GB".format(mb / 1024.0) else "$mb MB"

    /** chiavi dei nodi mostrati: la lista si ricostruisce solo se cambiano (ricostruirla faceva saltare la pagina) */
    private var nodesShown = listOf<String>()

    /** Voci dello stesso dispositivo (USB e Wi-Fi) raggruppate: una riga per nodo, con i suoi canali. */
    private fun nodeGroups(): List<List<Node>> = nodes.groupBy { it.devId.ifEmpty { it.endpoint } }.values.toList()

    private fun renderNodes() {
        var ram = 0L
        val sel = selectedNodes()
        val groups = nodeGroups()
        val keys = groups.map { g -> g.joinToString { it.endpoint } }
        if (keys != nodesShown) {
            nodesShown = keys
            nodesBox.removeAllViews()
            for (g in groups) addNodeRow(g)
        } else groups.forEachIndexed { i, g ->
            // stesso elenco: aggiorno solo il testo (attività, RAM) senza toccare la selezione
            (nodesBox.getChildAt(i) as? CheckBox)?.let { cb ->
                val t = withLinkIcons(g, nodeText(g))
                if (cb.text.toString() != t.toString()) cb.text = t
            }
        }
        // RAM contata una volta per dispositivo, anche se ha due canali
        sel.groupBy { it.devId.ifEmpty { it.endpoint } }.values.forEach { ram += it.first().freeMb }
        val devices = sel.map { it.devId.ifEmpty { it.endpoint } }.distinct().size
        nodesTitle.text = "Nodi trovati: ${groups.size} · selezionati $devices" +
            if (ram > 0) " · RAM libera ≈ ${"%.1f".format(ram / 1024.0)} GB" else ""
        renderCoordChoice()
    }

    /** Icone del canale davanti al nome: Wi-Fi, USB (tridente) o entrambe (si usa il più rapido, l'altro è di riserva). */
    private fun withLinkIcons(g: List<Node>, text: String): CharSequence = icons(
        buildList {
            if (g.any { it.role == "coordinatore" }) add(R.drawable.ic_home)
            if (g.any { !it.usb && !it.cable }) add(R.drawable.ic_wifi)
            if (g.any { it.usb || it.cable }) add(R.drawable.ic_usb) // adb o rete sul cavo (tethering, Ethernet)
        }, text)

    private fun icons(icons: List<Int>, text: CharSequence, sb: android.text.SpannableStringBuilder = android.text.SpannableStringBuilder()): CharSequence {
        val size = (resources.displayMetrics.scaledDensity * 18).toInt()
        for (res in icons) {
            val d = getDrawable(res)!!.apply { setBounds(0, 0, size, size) }
            val start = sb.length
            sb.append("\u00A0")
            sb.setSpan(android.text.style.ImageSpan(d, android.text.style.DynamicDrawableSpan.ALIGN_BASELINE),
                start, sb.length, android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            sb.append(" ")
        }
        return sb.append(text)
    }

    private fun nodeText(g: List<Node>): String {
        val n = g.first()
        val mem = if (n.ramMb > 0) " · RAM ${"%.1f".format(n.ramMb / 1024.0)} GB" else ""
        // il coordinatore ha l'icona della casa; gli altri ruoli restano scritti
        val role = g.map { it.role }.firstOrNull { it.isNotEmpty() && it != "coordinatore" }?.let { " · $it" }.orEmpty()
        // velocità misurata da qui (canale compreso) o, se manca, il bench che il nodo ha pubblicato su se stesso
        val speed = g.mapNotNull { Cluster.bench[it.endpoint] }.maxByOrNull { it.gbps }
            ?.let { " · ${"%.1f".format(it.gbps)} GB/s" }
            ?: g.firstOrNull { it.benchGbps > 0 }?.let { " · bench ${"%.1f".format(it.benchGbps)} GB/s ${"%.0f".format(it.benchGflops)} GFLOPS" }
            ?: ""
        // round-trip del canale più rapido (quello che userà il cluster)
        val rtt = g.mapNotNull { Cluster.bench[it.endpoint]?.rttMs }.minOrNull()?.let { " · rtt ${"%.0f".format(it)} ms" }.orEmpty()
        // attività vista dal suo /status: un coordinatore collegato al worker = il nodo sta lavorando per il cluster
        val act = g.maxByOrNull { it.clients }?.takeIf { it.clients > 0 }
            ?.let { "\n● in uso · pesi ${gb(it.workMb)} · CPU ${it.cpuPct}%" }.orEmpty()
        return "${n.name}\n${g.joinToString(" + ") { it.endpoint }}$mem" +
            "${if (n.cpu.isNotEmpty()) " · ${n.cpu}" else ""}$role$speed$rtt$act"
    }

    private fun addNodeRow(g: List<Node>) {
        val cb = CheckBox(this)
        cb.text = withLinkIcons(g, nodeText(g))
        cb.isChecked = g.none { it.endpoint in excluded }
        cb.setOnCheckedChangeListener { _, v ->
            g.forEach { if (v) excluded.remove(it.endpoint) else excluded.add(it.endpoint) }
            renderNodes()
        }
        nodesBox.addView(cb)
    }

    /** Menu del coordinatore: automatico, questo telefono, o un nodo che sa fare da coordinatore (telefoni, PC). */
    private fun renderCoordChoice() {
        val coordNodes = nodes.filter { it.canCoord && it.devId.isNotEmpty() && !isSelf(it, Native.localIp()) }
            .distinctBy { it.devId }
        val keys = listOf(Cluster.COORD_AUTO, Cluster.COORD_SELF) + coordNodes.map { it.devId }
        if (keys == coordKeys) return // non ricreo il menu a ogni aggiornamento: perderebbe la scelta aperta
        val labels = listOf("Automatico (PC se c'è, altrimenti il più veloce)", "Questo telefono") +
            coordNodes.map { "${it.name}${if (it.x86) " (PC)" else ""}" }
        coordKeys = keys
        coordChoice.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, labels)
        val saved = prefs.getString("coord", Cluster.COORD_AUTO)
        coordChoice.setSelection(keys.indexOf(saved).coerceAtLeast(0))
    }

    /** Il worker locale e i nodi selezionati, con la RAM e il ruolo aggiornati da /status. */
    private fun clusterNodes(): Pair<Node, List<Node>> {
        val myIp = Native.localIp()
        val self = Discovery.refresh(Node("127.0.0.1", Native.RPC_PORT, Native.deviceName()))
            .copy(name = "${Native.deviceName()} (questo)", freeMb = Native.memInfo(this).second)
        val others = selectedNodes().filter { !isSelf(it, myIp) }.filter { n ->
            Discovery.reachable(n).also { if (!it) NodeState.log("coord", "nodo non raggiungibile: ${n.endpoint}") }
        }.map { Discovery.refresh(it) }
        return self to others
    }

    private fun onBench() {
        if (NodeState.coordinatorRunning) return toast("Ferma prima l'LLM: i worker servono un client alla volta")
        toast("Misuro i nodi (circa 1 s ciascuno)…")
        thread(name = "bench") {
            val (self, others) = clusterNodes()
            Cluster(this).measure(listOf(self) + others)
            runOnUiThread { renderNodes() }
        }
    }

    private fun selectedNodes() = nodes.filter { it.endpoint !in excluded }

    /** Il worker di questo telefono. Un 127.0.0.1 su un'altra porta è un nodo via USB (es. il PC con adb reverse). */
    private fun isSelf(n: Node, myIp: String?) =
        n.host == myIp || (n.host == "127.0.0.1" && n.port == Native.RPC_PORT) ||
            // anche esposto da un PC (ponte USB "(USB del PC)" o "(via PC)"): stesso nome e RAM di questo telefono
            n.devId == selfDevId

    /** come Node.devId per questo telefono (dev/ram di StatusServer.status) */
    private val selfDevId by lazy { "${Native.deviceName()}/${Native.memInfo(this).first}" }

    // ---------------------------------------------------------------- modelli

    /** Modello che sta solo su un PC del cluster (tools/pcnode.py): si usa con quel PC come coordinatore, senza copiarlo qui. */
    data class RemoteModel(val name: String, val size: Long, val node: Node)

    private var modelLabels = listOf<String>()

    private fun refreshModels(select: File? = null) {
        val local = ModelStore.list(this)
        val missing = ModelStore.presets.filter { p -> local.none { it.name == p.url.substringAfterLast('/') } }
        val onPc = nodes.filter { it.canCoord && it.x86 && it.models.isNotEmpty() && !isSelf(it, Native.localIp()) }
            .sortedBy { if (it.usb) 1 else 0 } // a parità di PC il canale su rete (tethering, Wi-Fi), non adb
            .flatMap { n -> n.models.map { (name, size) -> RemoteModel(name, size, n) } }
            .filter { r -> local.none { it.name == r.name } }.distinctBy { it.name }
        val items = local + onPc + missing.filter { p -> onPc.none { it.name == p.url.substringAfterLast('/') } }
        val labels = items.map {
            when (it) {
                is File -> "📁 ${it.name} (${"%.1f".format(it.length() / 1e9)} GB)"
                is RemoteModel -> "🖥 ${it.name} (${"%.1f".format(it.size / 1e9)} GB, sul ${it.node.name.removeSuffix(" (USB)").removeSuffix(" (cavo)")})"
                else -> "⬇ ${(it as ModelStore.Preset).label}"
            }
        }
        if (select == null && labels == modelLabels) return // stesso elenco: non tocco lo spinner (perderebbe la scelta)
        val previous = modelLabels.getOrNull(modelSpinner.selectedItemPosition)
        modelItems = items
        modelLabels = labels
        modelSpinner.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, labels)
        val idx = select?.let { f -> modelItems.indexOfFirst { it is File && it.path == f.path } }
            ?: labels.indexOf(previous)
        if (idx >= 0) modelSpinner.setSelection(idx)
    }

    private fun onDownload() {
        if (downloading) { downloading = false; return }
        val url = customUrl.text.toString().trim().ifEmpty {
            (modelItems.getOrNull(modelSpinner.selectedItemPosition) as? ModelStore.Preset)?.url
        }
        if (url == null) { toast("Il modello selezionato è già sul telefono"); return }
        downloading = true
        downloadBtn.text = "Annulla download"
        progress.visibility = View.VISIBLE
        progress.isIndeterminate = true
        thread(name = "download") {
            val result = runCatching {
                ModelStore.download(this, url, { !downloading }) { done, total ->
                    runOnUiThread {
                        if (total > 0) {
                            progress.isIndeterminate = false
                            progress.max = 1000
                            progress.progress = (done * 1000 / total).toInt()
                        }
                        downloadBtn.text = "Annulla download (${done / 1048576} / ${if (total > 0) total / 1048576 else "?"} MB)"
                    }
                }
            }
            runOnUiThread {
                downloading = false
                downloadBtn.text = "Scarica modello"
                progress.visibility = View.GONE
                result.onSuccess { customUrl.text.clear(); refreshModels(it); toast("Scaricato ${it.name}") }
                    .onFailure { toast("Errore download: ${it.message}") }
            }
        }
    }

    // ---------------------------------------------------------- coordinatore

    private fun onCoordinator() {
        if (NodeState.coordinatorRunning) {
            thread { Cluster(this).stop() }
            NodeService.send(this, NodeService.ACTION_REPLICA_STOP)
            val hosts = replicaHosts
            replicaHosts = emptyList()
            thread { hosts.forEach { StatusServer.post(it, "/replica/stop") } }
            return
        }
        val item = modelItems.getOrNull(modelSpinner.selectedItemPosition)
        val remote = item as? RemoteModel
        val model = item as? File ?: remote?.let { File(cacheDir, it.name) }
            ?: return toast("Seleziona un modello già scaricato (📁) o presente sul PC (🖥)")
        val ctx = ctxField.text.toString().toIntOrNull()?.coerceIn(512, 131072) ?: 4096
        prefs.edit().putInt("ctx", ctx).apply()

        if (modeReplica.isChecked) {
            if (remote != null) return toast("Le repliche copiano il modello da questo telefono: scaricalo prima (📁)")
            return startReplicas(model, ctx)
        }
        val choice = prefs.getString("coord", Cluster.COORD_AUTO) ?: Cluster.COORD_AUTO
        coordBtn.isEnabled = false
        thread(name = "cluster") {
            val (self, others) = clusterNodes()
            val cluster = Cluster(this)
            // modello del PC: leggo solo l'intestazione e il PC fa da coordinatore con il suo file
            val remoteInfo = remote?.let { r ->
                NodeState.log("cluster", "${r.name} è sul ${r.node.name}: leggo l'intestazione, il PC farà da coordinatore")
                Cluster.remoteInfo(r.node, r.name, r.size, cacheDir)?.second?.let { it to r.node.devId }
                    ?: run { NodeState.log("cluster", "intestazione di ${r.name} non leggibile dal PC"); null }
            }
            val start = if (remote != null && remoteInfo == null) null
                else cluster.plan(model, ctx, self, others, choice, remoteInfo)
            runOnUiThread {
                coordBtn.isEnabled = true
                renderNodes()
                if (start == null) toast("Avvio non riuscito: vedi il log")
            }
            if (start != null) {
                // MoE veloce: solo per l'architettura provata (qwen3moe: 21/22 compiti con 6 esperti, come con 8)
                val experts = if (prefs.getBoolean("moefast", false) && start.info.arch == "qwen3moe") 6 else 0
                cluster.launch(start, experts = experts)
                // modello predittivo: solo se misura e stima dicono che conviene
                cluster.tuneSpeculative(start)
            }
        }
    }

    /**
     * Modalità repliche: il modello intero gira su ogni telefono in cui entra; i worker lo copiano
     * da questo telefono via LAN (/files) se non ce l'hanno, e il router smista gli utenti.
     */
    private fun startReplicas(model: File, ctx: Int) {
        val myIp = Native.localIp() ?: return toast("Nessuna rete")
        val candidates = selectedNodes().filter { it.host != myIp && it.host != "127.0.0.1" }
        coordBtn.isEnabled = false
        thread(name = "replicas") {
            val info = Gguf.read(model)
            fun fits(freeMb: Long) = info != null && SplitPlan.make(info, ctx * 2, freeMb, emptyList()) is SplitPlan.Ok
            val localOk = fits(Native.memInfo(this).second)
            val remote = candidates.map { Discovery.refresh(it) }.filter { n ->
                val ok = Discovery.reachable(n) && fits(n.freeMb)
                if (!ok) NodeState.log("coord", "${n.host} escluso: non raggiungibile o RAM insufficiente (${n.freeMb} MB liberi)")
                ok
            }
            val source = "http://$myIp:${Native.STATUS_PORT}/files/${Uri.encode(model.name)}"
            val started = remote.filter { n ->
                StatusServer.post(n.host, "/replica/start",
                    JSONObject().put("model", model.name).put("source", source).put("ctx", ctx))
            }.map { it.host }
            runOnUiThread {
                coordBtn.isEnabled = true
                if (info == null) return@runOnUiThread toast("File GGUF non leggibile")
                val hosts = (if (localOk) listOf("127.0.0.1") else emptyList()) + started
                if (hosts.isEmpty()) return@runOnUiThread toast("Il modello non entra in nessun telefono: usa la modalità Split")
                if (!localOk) NodeState.log("coord", "questo telefono non ha RAM per la replica: fa solo da router")
                replicaHosts = started
                Keepalive.pin(started)
                if (localOk) NodeService.send(this, NodeService.ACTION_REPLICA_START) {
                    putExtra(NodeService.EXTRA_MODEL, model.path)
                    putExtra(NodeService.EXTRA_CTX, ctx)
                }
                NodeService.send(this, NodeService.ACTION_ROUTER_START) {
                    putStringArrayListExtra(NodeService.EXTRA_HOSTS, ArrayList(hosts))
                    putExtra(NodeService.EXTRA_MODEL, model.name)
                }
                NodeState.log("coord", "repliche avviate su: ${hosts.joinToString()}")
            }
        }
    }

    // ---------------------------------------------------------------- varie

    @SuppressLint("BatteryLife")
    /** Chiude i processi in background delle altre app e mostra quanta RAM disponibile si è guadagnata (serve prima di caricare un modello). */
    private fun onFreeRam(btn: Button) {
        btn.isEnabled = false
        toast("Libero la RAM…")
        thread {
            val r = MemoryCleaner.free(this)
            runOnUiThread { btn.isEnabled = true; toast(r.describe()) }
        }
    }

    private fun askBatteryExemption() {
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) return toast("Già esclusa dall'ottimizzazione")
        startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
    }

    /** Alcuni telefoni (es. Huawei) vanno in standby e sospendono la rete anche con wake lock: lo schermo acceso lo evita. */
    private fun applyKeepScreen(on: Boolean) {
        if (on) window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        else window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
}
