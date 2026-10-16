package com.distai.app

import android.os.SystemClock
import android.system.Os
import android.system.OsConstants
import java.io.File

/**
 * Attività del worker (ggml-rpc-server) di questo telefono, per far vedere che sta lavorando:
 * coordinatori collegati (dalle righe "Accepted client connection" / "Client connection closed" del suo output),
 * memoria occupata dai pesi ricevuti e uso della CPU (da /proc del processo).
 */
object WorkerStats {
    @Volatile var clients = 0
        private set
    @Volatile var rssMb = 0L
        private set
    /** CPU in percentuale dei core assegnati al worker (100 = tutti i thread al lavoro) */
    @Volatile var cpuPct = 0
        private set

    private val pageSize by lazy { runCatching { Os.sysconf(OsConstants._SC_PAGESIZE) }.getOrDefault(4096L) }
    private val clkTck by lazy { runCatching { Os.sysconf(OsConstants._SC_CLK_TCK) }.getOrDefault(100L) }
    private var pid = 0
    private var lastTicks = 0L
    private var lastAt = 0L

    fun onLine(line: String) {
        when {
            line.startsWith("Accepted client connection") -> clients++
            line.startsWith("Client connection closed") -> clients = maxOf(0, clients - 1)
        }
    }

    fun reset() {
        clients = 0; rssMb = 0; cpuPct = 0; pid = 0; lastAt = 0
    }

    /** Aggiorna RAM e CPU del worker; la CPU è la media dall'ultimo campione (chiamata ogni pochi secondi). */
    @Synchronized
    fun sample() {
        if (!NodeState.workerRunning) return reset()
        if (pid == 0 || !File("/proc/$pid").exists()) pid = findPid() ?: return
        runCatching {
            rssMb = File("/proc/$pid/statm").readText().trim().split(' ')[1].toLong() * pageSize / 1048576
            // dopo il nome tra parentesi: stato è il campo 3, utime e stime i campi 14 e 15
            val f = File("/proc/$pid/stat").readText().substringAfterLast(')').trim().split(' ')
            val ticks = f[11].toLong() + f[12].toLong()
            val now = SystemClock.elapsedRealtime()
            if (lastAt in 1 until now) {
                val busyMs = (ticks - lastTicks) * 1000 / clkTck
                cpuPct = (busyMs * 100 / ((now - lastAt) * Native.bigCores())).toInt().coerceIn(0, 100)
            }
            lastTicks = ticks; lastAt = now
        }.onFailure { pid = 0 }
    }

    /** Processo figlio dell'app che esegue librpcserver_*.so (i propri processi in /proc sono leggibili). */
    private fun findPid(): Int? = File("/proc").listFiles()?.asSequence()
        ?.mapNotNull { it.name.toIntOrNull() }
        ?.firstOrNull { p -> runCatching { File("/proc/$p/cmdline").readText().contains("librpcserver") }.getOrDefault(false) }
}
