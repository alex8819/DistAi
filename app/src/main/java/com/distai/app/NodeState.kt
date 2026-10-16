package com.distai.app

import android.os.Handler
import android.os.Looper

/** Stato condiviso tra servizio e UI: processi attivi e log. */
object NodeState {
    @Volatile var workerRunning = false
    @Volatile var coordinatorRunning = false
    /** true quando llama-server ha caricato il modello e risponde alle richieste. */
    @Volatile var apiReady = false
    @Volatile var coordinatorInfo = ""
    @Volatile var replicaRunning = false
    @Volatile var replicaReady = false
    @Volatile var replicaState = "ferma"
    /** router attivo in modalità repliche: l'API è pronta se almeno una replica risponde */
    @Volatile var router: Router? = null
    /** ultimo piano del cluster (SplitPlan.describe): layer per nodo, stima e giudizio su ogni nodo */
    @Volatile var planInfo = ""

    private const val MAX_LINES = 500
    private val lines = ArrayDeque<String>()
    private val listeners = mutableSetOf<() -> Unit>()
    private val main = Handler(Looper.getMainLooper())

    fun log(tag: String, msg: String) {
        android.util.Log.i("DistAI", "[$tag] $msg") // anche nel logcat: si segue il cluster dal PC con adb
        synchronized(lines) {
            lines.addLast("[$tag] $msg")
            while (lines.size > MAX_LINES) lines.removeFirst()
        }
        notifyChanged()
    }

    fun logText(): String = synchronized(lines) { lines.joinToString("\n") }

    fun clearLog() {
        synchronized(lines) { lines.clear() }
        notifyChanged()
    }

    fun addListener(l: () -> Unit) = synchronized(listeners) { listeners.add(l) }
    fun removeListener(l: () -> Unit) = synchronized(listeners) { listeners.remove(l) }

    fun notifyChanged() {
        val snapshot = synchronized(listeners) { listeners.toList() }
        main.post { snapshot.forEach { it() } }
    }
}
