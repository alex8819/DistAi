package com.distai.app

import android.os.SystemClock
import android.system.Os
import android.system.OsConstants
import java.io.File

/** Uso della CPU del coordinatore (llama-server di questo telefono), misurato da /proc come in WorkerStats. */
object CoordStats {
    @Volatile var cpuPct = -1
        private set
    private val clkTck by lazy { runCatching { Os.sysconf(OsConstants._SC_CLK_TCK) }.getOrDefault(100L) }
    private var pid = 0
    private var lastTicks = 0L
    private var lastAt = 0L

    @Synchronized
    fun sample() {
        if (!NodeState.coordinatorRunning) { cpuPct = -1; pid = 0; lastAt = 0; return }
        if (pid == 0 || !File("/proc/$pid").exists()) pid = findPid() ?: return
        runCatching {
            val f = File("/proc/$pid/stat").readText().substringAfterLast(')').trim().split(' ')
            val ticks = f[11].toLong() + f[12].toLong()
            val now = SystemClock.elapsedRealtime()
            if (lastAt in 1 until now)
                cpuPct = ((ticks - lastTicks) * 1000 / clkTck * 100 / ((now - lastAt) * Native.bigCores())).toInt().coerceIn(0, 100)
            lastTicks = ticks; lastAt = now
        }.onFailure { pid = 0 }
    }

    private fun findPid(): Int? = File("/proc").listFiles()?.asSequence()
        ?.mapNotNull { it.name.toIntOrNull() }
        ?.firstOrNull { p -> runCatching { File("/proc/$p/cmdline").readText().contains("libllamaserver") }.getOrDefault(false) }
}
