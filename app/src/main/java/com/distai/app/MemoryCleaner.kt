package com.distai.app

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.provider.Settings
import java.io.File

/**
 * "Libera RAM": chiude i processi in background delle altre app per far salire MemAvailable prima di caricare un modello.
 * Android non lascia svuotare la cache di pagine senza root: l'unica leva legittima è killBackgroundProcesses (permesso normale),
 * che chiude solo processi NON in primo piano. Restano intatti questa app, il launcher, la tastiera, le app di sistema pure
 * (telefono, SMS, impostazioni, System UI) e qualunque app in primo piano.
 */
object MemoryCleaner {
    data class Result(val beforeKb: Long, val afterKb: Long, val killed: Int, val skipped: Int) {
        val gainKb get() = afterKb - beforeKb
        fun describe(): String = "RAM disponibile %.2f GB → %.2f GB (%+.2f GB), %d app chiuse in background, %d lasciate"
            .format(beforeKb / 1048576.0, afterKb / 1048576.0, gainKb / 1048576.0, killed, skipped)
    }

    /** MemAvailable di /proc/meminfo in kB (è la stima che usano SplitPlan e il bench dei nodi). */
    fun memAvailableKb(): Long = try {
        File("/proc/meminfo").useLines { ls ->
            ls.firstOrNull { it.startsWith("MemAvailable:") }?.split(Regex("\\s+"))?.getOrNull(1)?.toLongOrNull() ?: -1L
        }
    } catch (e: Exception) { -1L }

    /** Pacchetti da non toccare mai: launcher, tastiera attiva, telefono/SMS predefiniti, UI di sistema. */
    private fun protectedPackages(ctx: Context): Set<String> {
        val keep = mutableSetOf(ctx.packageName, "android", "com.android.systemui", "com.android.phone", "com.android.settings",
            "com.android.providers.telephony", "com.android.server.telecom", "com.google.android.gms", "com.google.android.gsf")
        val pm = ctx.packageManager
        pm.resolveActivity(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME), 0)?.activityInfo?.packageName?.let { keep += it }
        Settings.Secure.getString(ctx.contentResolver, Settings.Secure.DEFAULT_INPUT_METHOD)?.substringBefore('/')?.let { keep += it }
        return keep
    }

    /** Da chiamare fuori dal thread principale (attende ~1,5 s che Android recuperi la memoria). */
    fun free(ctx: Context): Result {
        val am = ctx.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        val protect = protectedPackages(ctx)
        val before = memAvailableKb()
        var killed = 0
        var skipped = 0
        for (app in ctx.packageManager.getInstalledApplications(0)) {
            val pkg = app.packageName
            val pureSystem = (app.flags and ApplicationInfo.FLAG_SYSTEM) != 0 && (app.flags and ApplicationInfo.FLAG_UPDATED_SYSTEM_APP) == 0
            if (pkg in protect || pureSystem || !app.enabled) { skipped++; continue }
            try { am.killBackgroundProcesses(pkg); killed++ } catch (e: SecurityException) { skipped++ }
        }
        System.gc()
        Thread.sleep(1500)
        val result = Result(before, memAvailableKb(), killed, skipped)
        NodeState.log("ram", result.describe())
        return result
    }
}
